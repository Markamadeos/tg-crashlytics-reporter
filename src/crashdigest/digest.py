"""Сборка текста сообщения. Без сети и диска — чистые функции."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from crashdigest.crashlytics import IssueRow

TELEGRAM_LIMIT = 4096
CONSOLE = "https://console.firebase.google.com/v1/appid/project"
MAX_LISTED = 30


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по правилам 1 / 2-4 / 5+."""
    if n % 100 in (11, 12, 13, 14):
        return many
    tail = n % 10
    if tail == 1:
        return one
    if tail in (2, 3, 4):
        return few
    return many


def issue_url(project: str, app_id: str, issue_id: str) -> str:
    return f"{CONSOLE}/{project}/crashlytics/app/{app_id}/issues/{issue_id}"


def _split_subtitle(subtitle: str) -> tuple[str, str]:
    """Делит 'Класс - текст' на части. Разделителя может не быть вовсе."""
    cls, sep, message = subtitle.partition(" - ")
    return (cls, message) if sep else (subtitle, "")


def _fmt_period(start: datetime, end: datetime) -> str:
    """Печатает оба конца окна в одной зоне — зоне `end`.

    `start` приходит из БД нормализованным в UTC, `end` — это `now` в
    локальной зоне. Печать «как есть» даёт разные зоны на разных концах
    строки: настоящее суточное окно превращалось в «25.08 07:00 → 26.08
    10:00», что читается как 27 часов.
    """
    start = start.astimezone(end.tzinfo)
    return f"{start.strftime('%d.%m %H:%M')} → {end.strftime('%d.%m %H:%M')}"


def _pack(header: list[str], blocks: list[list[str]]) -> list[str]:
    """Склеивает блоки в сообщения, не превышающие лимит Telegram."""
    messages: list[str] = []
    current = list(header)
    has_content = False

    for block in blocks:
        candidate = current + block
        if has_content and len("\n".join(candidate)) > TELEGRAM_LIMIT:
            messages.append("\n".join(current))
            current = list(block)
        else:
            current = candidate
            has_content = True

    if current:
        messages.append("\n".join(current))
    return messages


def render(
    rows: Sequence[IssueRow],
    start: datetime,
    end: datetime,
    *,
    app_name: str,
    project: str,
    app_id: str,
    truncated: bool = False,
    scanned: int | None = None,
    versions: Sequence[str] | None = None,
) -> list[str]:
    period = _fmt_period(start, end)

    # Шапка обязана называть срез: без неё «2 новых» нечем интерпретировать,
    # а смена отслеживаемых версий видна только в конфиге.
    versions_line = f"на версиях {html.escape(', '.join(versions))}" if versions else ""

    if not rows:
        # «Новых падений нет» неотличимо от «фильтр сломан и вернул 0 строк».
        # Печатаем срез и сколько issue в нём известно: пустым сообщение
        # бывает только когда новых ноль, значит все увиденные — известные.
        parts = [f"✅ <b>{html.escape(app_name)}</b>: новых падений нет"]
        scope = [p for p in (versions_line,) if p]
        if scanned is not None:
            scope.append(
                f"{scanned} {plural(scanned, 'известный', 'известных', 'известных')}"
            )
        if scope:
            parts.append(" · ".join(scope))
        parts.append(f"<i>{period}</i>")
        return ["\n".join(parts)]

    header = [f"🔴 <b>Новые падения {html.escape(app_name)} — {len(rows)}</b>"]
    if versions_line:
        header.append(versions_line)
    header.append(f"<i>{period}</i>")
    if truncated:
        header.append(
            "⚠️ <i>разрыв наблюдения: окно обрезано до 90 дней, "
            "часть периода не покрыта</i>"
        )
    header.append("")

    # Без границы одна раздутая подпись без разделителя (класс = вся
    # строка) или тысячи issue в одном окне дают сообщение длиннее лимита
    # Telegram → неретраибельный HTTP 400 → состояние не продвигается →
    # тот же issue снова и снова во всё расширяющемся окне, дайджест
    # застревает навсегда. Перечисляем не больше MAX_LISTED, они уже
    # отсортированы по числу событий, самые частые — первые.
    listed = rows[:MAX_LISTED]
    omitted = len(rows) - len(listed)

    blocks: list[list[str]] = []
    for index, row in enumerate(listed, 1):
        cls, message = _split_subtitle(row.subtitle)
        block = [f"{index}. <b>{html.escape(cls[:200])}</b>"]
        if message:
            block.append(f"   {html.escape(message[:200])}")
        block.append(
            f"   {row.error_type} · "
            f"{row.events_count} {plural(row.events_count, 'событие', 'события', 'событий')} · "
            f"{row.impacted_users} {plural(row.impacted_users, 'юзер', 'юзера', 'юзеров')}"
        )
        url = issue_url(project, app_id, row.issue_id)
        block.append(f'   <a href="{url}">открыть в Crashlytics</a>')
        block.append("")
        blocks.append(block)

    if omitted:
        blocks.append([
            f"… и ещё {omitted} {plural(omitted, 'issue', 'issue', 'issue')} "
            "— см. консоль Crashlytics"
        ])

    return _pack(header, blocks)


def render_bootstrap(count: int, app_name: str) -> list[str]:
    return [
        f"🧭 <b>{html.escape(app_name)}</b>: инициализация\n"
        f"Запомнено {count} {plural(count, 'issue', 'issue', 'issue')}, "
        f"дальше сообщаю только о новых."
    ]


def render_error(headline: str, detail: str) -> list[str]:
    return [f"⚠️ <b>{html.escape(headline)}</b>\n<code>{html.escape(detail[:600])}</code>"]


@dataclass(frozen=True)
class Growth:
    row: IssueRow
    was_events: int

    @property
    def delta(self) -> int:
        return self.row.events_count - self.was_events


@dataclass(frozen=True)
class WeekTotals:
    events: int
    users: int


def week_totals(rows: Sequence[IssueRow]) -> WeekTotals:
    """Суммы за неделю.

    Пользователи складываются по issue, а не считаются как различные: API
    отдаёт impactedUsersCount только в разбивке по issue, и человек, упавший
    в трёх issue, посчитан трижды. Как абсолют цифра завышена, как сравнение
    двух недель корректна — перекос одинаков в обоих числах.
    """
    return WeekTotals(
        events=sum(r.events_count for r in rows),
        users=sum(r.impacted_users for r in rows),
    )


def weekly_growth(
    current: Sequence[IssueRow], previous: Sequence[IssueRow]
) -> list[Growth]:
    """Растущие issue, от большего прироста к меньшему.

    Ранжирование по АБСОЛЮТНОМУ приросту: рост с 2 до 20 даёт десятикратное
    отношение и вытеснил бы рост с 20 до 64, который затрагивает на два
    порядка больше людей.
    """
    was = {r.issue_id: r.events_count for r in previous}
    growth = [
        Growth(row=r, was_events=was.get(r.issue_id, 0))
        for r in current
        if r.events_count > was.get(r.issue_id, 0)
    ]
    growth.sort(key=lambda g: g.delta, reverse=True)
    return growth


def _fmt_week(period: tuple[datetime, datetime]) -> str:
    start, end = period
    start = start.astimezone(end.tzinfo)
    return f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}"


def _fmt_change(was: int, now: int) -> str:
    return f"{was} → {now} · {now - was:+d}"


def render_weekly(
    growth: Sequence[Growth],
    *,
    app_name: str,
    project: str,
    app_id: str,
    versions: Sequence[str],
    previous: tuple[datetime, datetime],
    current: tuple[datetime, datetime],
    prev_totals: WeekTotals,
    cur_totals: WeekTotals,
    top: int,
) -> list[str]:
    arrow = "📈" if cur_totals.events > prev_totals.events else "📉"
    header = [
        f"{arrow} <b>Фон за неделю — {html.escape(app_name)}</b>",
        f"на версиях {html.escape(', '.join(versions))}",
        f"<i>{_fmt_week(previous)} → {_fmt_week(current)}</i>",
        f"события {_fmt_change(prev_totals.events, cur_totals.events)}",
        f"юзеров {_fmt_change(prev_totals.users, cur_totals.users)}",
        "",
    ]

    if not growth:
        return ["\n".join(header + ["Растущих нет — фон не увеличивается."])]

    listed = list(growth[:top])
    header.append(f"Растут сильнее всего — {len(listed)} из {len(growth)}:")
    header.append("")

    blocks: list[list[str]] = []
    for index, item in enumerate(listed, 1):
        row = item.row
        cls, message = _split_subtitle(row.subtitle)
        block = [f"{index}. <b>{html.escape(cls[:200])}</b>"]
        if message:
            block.append(f"   {html.escape(message[:200])}")
        # «известен с», а не «стреляет с»: firstSeenVersion глобален для issue
        # и означает «известен с незапамятных времён», а не «начал недавно».
        block.append(
            f"   известен с {html.escape(row.first_seen_version)} · "
            f"было {item.was_events} → стало {row.events_count} · "
            f"{row.impacted_users} {plural(row.impacted_users, 'юзер', 'юзера', 'юзеров')} · "
            f"+{item.delta}"
        )
        url = issue_url(project, app_id, row.issue_id)
        block.append(f'   <a href="{url}">открыть в Crashlytics</a>')
        block.append("")
        blocks.append(block)

    return _pack(header, blocks)
