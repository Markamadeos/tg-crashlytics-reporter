"""Локальное состояние: какие issue уже видели и когда был успешный запуск."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Sequence

from crashdigest.crashlytics import IssueRow

DEFAULT_WINDOW = timedelta(days=1)
WINDOW_CAP = timedelta(days=90)
LAST_SUCCESS_KEY = "last_success_at"
LAST_WEEKLY_KEY = "last_weekly_at"

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
  issue_id      TEXT PRIMARY KEY,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  last_state    TEXT,
  error_type    TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _require_aware(value: datetime, name: str) -> None:
    """Наивное время — ошибка, а не молчаливое допущение про UTC."""
    if value.tzinfo is None:
        raise ValueError(f"{name} должен быть timezone-aware, получено {value!r}")


def _iso(value: datetime) -> str:
    _require_aware(value, "datetime")
    return value.astimezone(timezone.utc).isoformat()


class State:
    def __init__(self, path: str):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def window(self, now: datetime) -> tuple[datetime, datetime, bool]:
        """Возвращает (начало, конец, обрезано_ли) окна наблюдения."""
        _require_aware(now, "now")
        raw = self._get_meta(LAST_SUCCESS_KEY)
        if raw is None:
            return now - DEFAULT_WINDOW, now, False

        start = datetime.fromisoformat(raw)
        # Часы NAS сбросились до синхронизации по NTP → last_success_at в
        # будущем → start > end → Crashlytics отвечает 400. Не позволяем
        # окну стать отрицательным.
        start = min(start, now)
        if now - start > WINDOW_CAP:
            return now - WINDOW_CAP, now, True
        return start, now, False

    def diff_new(self, rows: Sequence[IssueRow]) -> list[IssueRow]:
        if not rows:
            return []
        known = {
            row[0]
            for row in self._db.execute(
                "SELECT issue_id FROM issues WHERE issue_id IN (%s)"
                % ",".join("?" * len(rows)),
                [r.issue_id for r in rows],
            )
        }
        return [r for r in rows if r.issue_id not in known]

    def record(self, rows: Sequence[IssueRow], now: datetime) -> None:
        stamp = _iso(now)
        self._db.executemany(
            """
            INSERT INTO issues (issue_id, first_seen_at, last_seen_at, last_state, error_type)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                last_state   = excluded.last_state
            """,
            [(r.issue_id, stamp, stamp, r.state, r.error_type) for r in rows],
        )
        self._db.commit()

    def mark_success(self, at: datetime) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LAST_SUCCESS_KEY, _iso(at)),
        )
        self._db.commit()

    def last_success_at(self) -> datetime | None:
        """Момент последнего успешного прогона — нужен для стартовой досылки."""
        raw = self._get_meta(LAST_SUCCESS_KEY)
        return None if raw is None else datetime.fromisoformat(raw)

    def last_weekly_at(self) -> datetime | None:
        """Момент последней отправки недельного отчёта — чтобы пропущенный
        понедельник не терялся бесследно, а перезапуск в понедельник не
        слал отчёт повторно."""
        raw = self._get_meta(LAST_WEEKLY_KEY)
        return datetime.fromisoformat(raw) if raw else None

    def mark_weekly_sent(self, at: datetime) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LAST_WEEKLY_KEY, _iso(at)),
        )
        self._db.commit()

    def known_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM issues").fetchone()[0]

    def seen_at(self, issue_id: str) -> tuple[str, str]:
        """(first_seen_at, last_seen_at) — чтобы тесты могли проверить инвариант."""
        row = self._db.execute(
            "SELECT first_seen_at, last_seen_at FROM issues WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        if row is None:
            raise KeyError(issue_id)
        return row[0], row[1]

    def _get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
