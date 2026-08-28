"""Порядок релизов и выбор последних N версий."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class AppVersion:
    display_version: str
    build_version: str
    display_name: str


@dataclass(frozen=True)
class RecentVersions:
    display_versions: tuple[str, ...]
    display_names: tuple[str, ...]


def build_rank(build_version: str) -> int:
    """Ранг билда для сортировки по свежести.

    Единственный доступный признак свежести: поле Version.tracks API не
    заполняет. В наблюдаемых данных сосуществуют две схемы билдов —
    десятизначная YYYYMMDDNN и пятизначная последовательная, — и числовое
    сравнение упорядочивает их правильно, потому что десятизначные всегда
    больше. Нечисловой билд не должен ронять сортировку, поэтому получает
    заведомо низший ранг.

    `str.isdigit()` — ловушка: для "²" оно возвращает True, а `int("²")`
    бросает ValueError, и вместо заявленного отката на -1 сортировка уронила
    бы весь прогон. `isascii()` отсекает такие символы раньше `int()`.
    """
    return int(build_version) if build_version.isascii() and build_version.isdigit() else -1


def recent_versions(versions: Sequence[AppVersion], count: int) -> RecentVersions:
    """N самых свежих displayVersion и ВСЕ их билды для фильтра запроса."""
    if count <= 0:
        raise ValueError(f"count должен быть положительным, получено {count}")

    newest: dict[str, int] = {}
    builds: dict[str, list[str]] = {}
    for version in versions:
        rank = build_rank(version.build_version)
        newest[version.display_version] = max(
            newest.get(version.display_version, rank), rank
        )
        builds.setdefault(version.display_version, []).append(version.display_name)

    chosen = sorted(newest, key=lambda dv: newest[dv], reverse=True)[:count]
    names = tuple(name for dv in chosen for name in builds[dv])
    return RecentVersions(display_versions=tuple(chosen), display_names=names)
