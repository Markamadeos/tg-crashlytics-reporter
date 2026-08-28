from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from crashdigest import scheduler
from crashdigest.scheduler import next_run_at, resolved_zone_name, run_forever

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


def test_next_run_is_today_when_time_not_passed():
    now = datetime(2026, 8, 26, 9, 0, tzinfo=MSK)
    assert next_run_at(now, "0 10 * * *") == datetime(2026, 8, 26, 10, 0, tzinfo=MSK)


def test_next_run_rolls_over_midnight():
    now = datetime(2026, 8, 26, 23, 30, tzinfo=MSK)
    assert next_run_at(now, "0 10 * * *") == datetime(2026, 8, 27, 10, 0, tzinfo=MSK)


def test_next_run_keeps_wall_clock_across_dst_change():
    """В ночь на 2026-03-29 Берлин переходит на летнее время."""
    now = datetime(2026, 3, 28, 12, 0, tzinfo=BERLIN)
    first = next_run_at(now, "0 10 * * *")
    second = next_run_at(first, "0 10 * * *")
    assert first.hour == 10
    assert second.hour == 10
    assert second.date() == datetime(2026, 3, 30, tzinfo=BERLIN).date()


def test_run_forever_executes_job_at_due_time_then_stops():
    now = [datetime(2026, 8, 26, 9, 59, 55, tzinfo=MSK)]
    runs = []
    stop = [False]

    def job():
        runs.append(now[0])
        stop[0] = True

    def sleep(seconds):
        now[0] = now[0] + timedelta(seconds=seconds)

    run_forever(
        job, "0 10 * * *",
        clock=lambda: now[0], sleep=sleep,
        should_stop=lambda: stop[0], on_error=lambda exc: None,
    )
    assert len(runs) == 1


def test_run_forever_survives_job_failure():
    now = [datetime(2026, 8, 26, 9, 59, 55, tzinfo=MSK)]
    errors = []
    calls = []

    def job():
        calls.append(1)
        raise RuntimeError("boom")

    def sleep(seconds):
        now[0] = now[0] + timedelta(seconds=seconds)

    run_forever(
        job, "0 10 * * *",
        clock=lambda: now[0], sleep=sleep,
        should_stop=lambda: len(calls) >= 2,
        on_error=errors.append,
    )
    assert len(calls) == 2
    assert all(isinstance(e, RuntimeError) for e in errors)


def test_default_clock_returns_live_zone_not_fixed_offset(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    now = scheduler._local_clock()
    assert isinstance(now.tzinfo, ZoneInfo)
    assert now.tzinfo.key == "Europe/Berlin"


def test_resolved_zone_name_reports_the_tz_key(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert resolved_zone_name() == "Europe/Berlin"


def test_resolved_zone_name_is_honest_when_not_a_zoneinfo(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(scheduler, "_local_zone", lambda: timezone.utc)
    assert resolved_zone_name() == str(timezone.utc)


def test_next_run_adjusts_offset_across_dst_fallback():
    """Осенний переход: следующий срок обязан приехать уже в зимнем смещении."""
    now = datetime(2026, 10, 24, 14, 0, tzinfo=BERLIN)
    nxt = next_run_at(now, "0 10 * * *")
    assert nxt.hour == 10
    assert nxt.utcoffset() == timedelta(hours=1)
