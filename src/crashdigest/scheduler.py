"""Планировщик: спит короткими тиками до очередного срока по cron-выражению."""
from __future__ import annotations

import os
import signal
import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

TICK_SECONDS = 5


def _local_zone():
    """Живая зона из TZ, а не фиксированное смещение."""
    name = os.environ.get("TZ")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo


def resolved_zone_name() -> str:
    """Имя зоны, в которую в реальности разрешился `_local_zone()`.

    Нужно `main` для стартового сообщения: молчаливый откат к UTC
    (например, из-за опечатки в TZ) иначе никак не виден.
    """
    zone = _local_zone()
    return zone.key if isinstance(zone, ZoneInfo) else str(zone)


def _local_clock() -> datetime:
    """Текущее локальное время в САМОЙ зоне, а не в её сегодняшнем смещении.

    `datetime.now().astimezone()` возвращает fixed-offset `datetime.timezone`.
    `next_run_at` приклеивает `now.tzinfo` к следующему сроку, и через переход
    на зимнее время это было бы устаревшее смещение: от 24.10.2026 14:00 CEST
    с живой зоной следующий запуск — 25.10 10:00+01:00, с фиксированным
    смещением — 10:00+02:00, то есть на час раньше по UTC. Проверено.
    """
    return datetime.now(_local_zone())


def next_run_at(now: datetime, schedule: str) -> datetime:
    """Ближайший момент после now, удовлетворяющий cron-выражению.

    Считаем по локальным настенным часам: croniter получает НАИВНОЕ время,
    зона возвращается на место после. Передавать croniter aware-datetime
    нельзя — на переходе DST он сдвигает час: 28.03.2026 12:00 CET даёт
    29.03.2026 09:00 CEST вместо 10:00, а следующим шагом ещё и 10:00 того
    же дня, то есть два запуска за сутки.
    """
    if now.tzinfo is None:
        raise ValueError("now должен быть timezone-aware")
    nxt = croniter(schedule, now.replace(tzinfo=None)).get_next(datetime)
    return nxt.replace(tzinfo=now.tzinfo)


def install_sigterm_handler() -> Callable[[], bool]:
    """Возвращает предикат «пора останавливаться», взведённый по SIGTERM/SIGINT."""
    stopping = False

    def handler(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    return lambda: stopping


def run_forever(
    job: Callable[[], None],
    schedule: str,
    *,
    clock: Callable[[], datetime] = None,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
    on_error: Callable[[BaseException], None] = lambda exc: None,
) -> None:
    """Крутится, пока should_stop() не станет истиной.

    Ошибку задачи отдаёт в on_error и продолжает: умирать нельзя, иначе
    контейнер уйдёт в цикл перезапусков и мониторинг прекратится молча.
    """
    clock = clock or _local_clock
    due = next_run_at(clock(), schedule)

    while not should_stop():
        now = clock()
        if now >= due:
            try:
                job()
            except Exception as exc:  # noqa: BLE001 — цикл обязан пережить любую
                on_error(exc)
            due = next_run_at(clock(), schedule)
            continue
        sleep(TICK_SECONDS)
