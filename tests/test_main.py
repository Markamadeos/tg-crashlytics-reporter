from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from crashdigest.config import Config
from crashdigest.crashlytics import CrashlyticsError, IssueRow
from crashdigest.main import (
    Deps,
    _catch_up_due,
    _run_startup_catch_up,
    _send_startup_message,
    _startup_text,
    _weekly_due,
    build_deps,
    run_once,
)
from crashdigest.state import State
from crashdigest.versions import AppVersion

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)

CFG = Config(
    project="example-prod", app_id="app-1",
    credentials_path="/secrets/google-service-account.json",
    bot_token="bt", chat_id="-100", proxy=None,
    error_types=("FATAL", "ANR"), schedule="0 10 * * *",
    state_path=":memory:", app_name="Example",
    version_window=3, weekly_top=5, weekly_weekday=0,
)


def row(issue_id):
    return IssueRow(
        issue_id=issue_id, title="t", subtitle="cls - msg", error_type="FATAL",
        state="OPEN", last_seen_version="5.0.0", first_seen_version="5.0.0",
        events_count=5, impacted_users=3,
    )


DEFAULT_FAKE_VERSIONS = [AppVersion("1.0.0", "1", "1.0.0 (1)")]


class FakeCrashlytics:
    """Версии по умолчанию непустые: `_tracked_versions` теперь считается на
    каждом пути (включая bootstrap) и останавливает прогон, если каталог
    версий пуст — тесты этого файла, которым сам список версий безразличен,
    не должны спотыкаться об эту проверку.
    """

    def __init__(self, rows=None, error=None, versions=None):
        self.rows = rows or []
        self.error = error
        self.calls = []
        self.versions = DEFAULT_FAKE_VERSIONS if versions is None else versions

    def top_issues(self, start, end, error_types, versions=None):
        self.calls.append((start, end, error_types))
        if self.error:
            raise self.error
        return self.rows

    def list_versions(self, start, end):
        return self.versions


class FakeTelegram:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send(self, messages):
        if self.error:
            raise self.error
        self.sent.extend(messages)

    def send_plain(self, messages):
        if self.error:
            raise self.error
        self.sent.extend(messages)


@pytest.fixture
def state(tmp_path):
    st = State(str(tmp_path / "s.db"))
    yield st
    st.close()


def test_new_issues_are_sent_and_recorded(state):
    tg = FakeTelegram()
    deps = Deps(crashlytics=FakeCrashlytics([row("a")]), state=state, telegram=tg)

    run_once(CFG, deps, now=NOW)

    assert tg.sent and "cls" in tg.sent[0]
    assert state.diff_new([row("a")]) == []
    assert state.window(NOW + timedelta(days=1))[0] == NOW


def test_second_run_reports_nothing_new(state):
    deps = Deps(crashlytics=FakeCrashlytics([row("a")]), state=state, telegram=FakeTelegram())
    run_once(CFG, deps, now=NOW)

    tg = FakeTelegram()
    deps2 = Deps(crashlytics=FakeCrashlytics([row("a")]), state=state, telegram=tg)
    run_once(CFG, deps2, now=NOW + timedelta(days=1))

    assert "нет" in tg.sent[0].lower()


def test_failed_delivery_does_not_advance_window(state):
    tg = FakeTelegram(error=RuntimeError("telegram down"))
    deps = Deps(crashlytics=FakeCrashlytics([row("a")]), state=state, telegram=tg)

    with pytest.raises(RuntimeError):
        run_once(CFG, deps, now=NOW)

    start, _, _ = state.window(NOW + timedelta(hours=1))
    assert start == NOW + timedelta(hours=1) - timedelta(days=1)


def test_api_failure_is_reported_to_channel_and_reraised(state):
    tg = FakeTelegram()
    deps = Deps(
        crashlytics=FakeCrashlytics(error=CrashlyticsError("HTTP 403: denied")),
        state=state, telegram=tg,
    )

    with pytest.raises(CrashlyticsError):
        run_once(CFG, deps, now=NOW)

    assert tg.sent and "403" in tg.sent[0]


def test_bootstrap_records_without_listing(state):
    tg = FakeTelegram()
    rows = [row(f"id{i}") for i in range(5)]
    deps = Deps(crashlytics=FakeCrashlytics(rows), state=state, telegram=tg)

    run_once(CFG, deps, now=NOW, bootstrap=True)

    assert "5" in tg.sent[0]
    assert "id0" not in tg.sent[0]
    assert state.diff_new(rows) == []


def test_failed_bootstrap_delivery_records_nothing(state):
    rows = [row(f"id{i}") for i in range(3)]
    tg = FakeTelegram(error=RuntimeError("telegram down"))
    deps = Deps(crashlytics=FakeCrashlytics(rows), state=state, telegram=tg)

    with pytest.raises(RuntimeError):
        run_once(CFG, deps, now=NOW, bootstrap=True)

    assert state.known_count() == 0          # ничего не запомнили
    assert state.diff_new(rows) == rows      # всё ещё новое


def test_bootstrap_uses_ninety_day_window(state):
    crash = FakeCrashlytics([])
    deps = Deps(crashlytics=crash, state=state, telegram=FakeTelegram())

    run_once(CFG, deps, now=NOW, bootstrap=True)

    start, end, error_types = crash.calls[0]
    assert (end - start).days == 90
    assert error_types == ("FATAL", "ANR")


def test_build_deps_uses_provided_telegram_when_given(service_account_key):
    sentinel = FakeTelegram()
    deps = build_deps(replace(CFG, credentials_path=service_account_key), telegram=sentinel)
    try:
        assert deps.telegram is sentinel
    finally:
        deps.state.close()


def test_build_deps_builds_default_telegram_when_not_given(service_account_key):
    deps = build_deps(replace(CFG, credentials_path=service_account_key))
    try:
        assert deps.telegram is not None
        assert deps.telegram is not deps.crashlytics
    finally:
        deps.state.close()


def test_catch_up_due_when_never_succeeded():
    assert _catch_up_due(None, NOW, "0 10 * * *") is True


def test_catch_up_due_when_next_scheduled_run_already_passed():
    last = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 26, 11, 0, tzinfo=timezone.utc)  # прогон 25-го пропущен
    assert _catch_up_due(last, now, "0 10 * * *") is True


def test_catch_up_not_due_when_next_scheduled_run_still_ahead():
    last = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)
    now = datetime(2026, 8, 26, 9, 30, tzinfo=timezone.utc)
    assert _catch_up_due(last, now, "0 10 * * *") is False


def test_startup_text_reports_never_succeeded():
    text = _startup_text(CFG, zone="Europe/Moscow", now=NOW, last_success=None)
    assert "Europe/Moscow" in text
    assert "0 10 * * *" in text
    assert "ещё не было" in text


def test_startup_text_reports_last_success_timestamp():
    last = NOW - timedelta(days=1)
    text = _startup_text(CFG, zone="Europe/Moscow", now=NOW, last_success=last)
    assert last.astimezone(NOW.tzinfo).isoformat(timespec="seconds") in text


def test_send_startup_message_reports_schedule(state):
    tg = FakeTelegram()
    deps = Deps(crashlytics=FakeCrashlytics(), state=state, telegram=tg)

    _send_startup_message(CFG, deps, now=NOW)

    assert tg.sent and "0 10 * * *" in tg.sent[0]


def test_send_startup_message_failure_does_not_raise(state):
    tg = FakeTelegram(error=RuntimeError("down"))
    deps = Deps(crashlytics=FakeCrashlytics(), state=state, telegram=tg)

    _send_startup_message(CFG, deps, now=NOW)  # не должно бросить наружу


def test_startup_catch_up_runs_job_when_never_succeeded(state):
    tg = FakeTelegram()
    crash = FakeCrashlytics([row("a")])
    deps = Deps(crashlytics=crash, state=state, telegram=tg)

    _run_startup_catch_up(CFG, deps, now=NOW)

    assert crash.calls
    assert tg.sent


def test_startup_catch_up_skips_when_not_overdue(state):
    state.mark_success(NOW - timedelta(minutes=5))
    tg = FakeTelegram()
    crash = FakeCrashlytics([row("a")])
    deps = Deps(crashlytics=crash, state=state, telegram=tg)

    _run_startup_catch_up(CFG, deps, now=NOW - timedelta(minutes=4))

    assert not crash.calls


def test_startup_catch_up_failure_does_not_prevent_daemon_start(state):
    tg = FakeTelegram(error=RuntimeError("down"))
    crash = FakeCrashlytics([row("a")])
    deps = Deps(crashlytics=crash, state=state, telegram=tg)

    _run_startup_catch_up(CFG, deps, now=NOW)  # не бросает наружу


def test_main_reports_build_deps_failure_to_channel_and_exits_1(monkeypatch):
    """`State(cfg.state_path)` может упасть (/data read-only), и это не
    должно кончиться голым traceback + перезапуском по кругу.
    """
    monkeypatch.setenv("CRASHLYTICS_PROJECT", "p")
    monkeypatch.setenv("CRASHLYTICS_APP_ID", "a")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/google-service-account.json")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bt")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")

    sent = []

    class FakeTG:
        def __init__(self, *a, **kw):
            pass

        def send(self, messages):
            sent.extend(messages)

        def send_plain(self, messages):
            sent.extend(messages)

    def boom(cfg, *, telegram=None):
        raise RuntimeError("disk is read-only")

    monkeypatch.setattr("crashdigest.main.Telegram", FakeTG)
    monkeypatch.setattr("crashdigest.main.build_deps", boom)

    import crashdigest.main as main_mod

    code = main_mod.main(["--once"])

    assert code == 1
    assert sent and "disk is read-only" in sent[0]


def test_build_deps_failure_reports_through_send_plain_not_send(monkeypatch):
    """`main.py:298` шлёт «контейнер не запустился» через sendMessage, а не
    rich: это может быть отказ именно rich-формата, и его обязан пережить
    другой канал доставки.

    В отличие от `test_main_reports_build_deps_failure_to_channel_and_exits_1`,
    FakeTG здесь считает вызовы `send` и `send_plain` раздельно — общий
    список `sent` не различает, каким методом ушло сообщение.
    """
    monkeypatch.setenv("CRASHLYTICS_PROJECT", "p")
    monkeypatch.setenv("CRASHLYTICS_APP_ID", "a")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/google-service-account.json")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bt")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")

    calls = {"rich": 0, "plain": 0}

    class FakeTG:
        def __init__(self, *a, **kw):
            pass

        def send(self, messages):
            calls["rich"] += 1

        def send_plain(self, messages):
            calls["plain"] += 1

    def boom(cfg, *, telegram=None):
        raise RuntimeError("disk is read-only")

    monkeypatch.setattr("crashdigest.main.Telegram", FakeTG)
    monkeypatch.setattr("crashdigest.main.build_deps", boom)

    import crashdigest.main as main_mod

    code = main_mod.main(["--once"])

    assert code == 1
    assert calls["plain"] == 1, "сбой старта обязан идти через sendMessage"
    assert calls["rich"] == 0, "sendRichMessage мог сломаться сам — им нельзя сообщать о его же отказе"


class FakeCrashlyticsWithVersions:
    def __init__(self, versions, issues_by_call=None, error=None):
        self.versions = versions
        self.issues_by_call = issues_by_call or []
        self.error = error
        self.calls = []
        self.version_calls = []

    def list_versions(self, start, end):
        self.version_calls.append((start, end))
        return self.versions

    def top_issues(self, start, end, error_types, versions=None):
        self.calls.append((start, end, error_types, versions))
        if self.error:
            raise self.error
        if self.issues_by_call:
            return self.issues_by_call.pop(0)
        return []


def av(display, build):
    return AppVersion(display, build, f"{display} ({build})")


VERSIONS = [av("5.15.0", "2026082001"), av("5.14.0", "2026072901"),
            av("5.13.1", "2026063001"), av("4.7.0", "10430")]


def test_daily_run_filters_by_recent_versions(state):
    crash = FakeCrashlyticsWithVersions(VERSIONS, [[row("a")]])
    tg = FakeTelegram()
    run_once(CFG, Deps(crashlytics=crash, state=state, telegram=tg), now=NOW)

    _, _, _, versions = crash.calls[0]
    assert versions == [
        "5.15.0 (2026082001)",
        "5.14.0 (2026072901)",
        "5.13.1 (2026063001)",
    ]
    assert "5\\.15\\.0, 5\\.14\\.0, 5\\.13\\.1" in tg.sent[0]


def test_weekly_report_sent_on_configured_weekday(state):
    """В понедельник уходит недельный отчёт, и версии в нём в обеих формах."""
    monday = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    assert monday.weekday() == 0
    crash = FakeCrashlyticsWithVersions(VERSIONS, [[row("a")], [row("a")], []])
    tg = FakeTelegram()

    run_once(CFG, Deps(crashlytics=crash, state=state, telegram=tg), now=monday)

    assert any("Фон за неделю" in m for m in tg.sent)

    # Оба недельных запроса, как и дневной, обязаны уйти с билд-номерами:
    # без них фильтр не сузит выборку и числа «было → стало» посчитаются
    # по всем версиям.
    expected = [
        "5.15.0 (2026082001)",
        "5.14.0 (2026072901)",
        "5.13.1 (2026063001)",
    ]
    daily_call, weekly_cur_call, weekly_prev_call = crash.calls[:3]
    assert daily_call[3] == expected
    assert weekly_cur_call[3] == expected
    assert weekly_prev_call[3] == expected

    # А в сообщении — голые версии без билдов.
    # В новом Markdown-формате версии с экранированными точками: 5\.15\.0, 5\.14\.0, 5\.13\.1
    # This matches the sibling daily test's strength: check the complete list, not just first version.
    assert "5\\.15\\.0, 5\\.14\\.0, 5\\.13\\.1" in tg.sent[1]


def test_weekly_report_not_sent_on_other_days(state):
    tuesday = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    assert tuesday.weekday() == 1
    crash = FakeCrashlyticsWithVersions(VERSIONS, [[row("a")]])
    tg = FakeTelegram()

    run_once(CFG, Deps(crashlytics=crash, state=state, telegram=tg), now=tuesday)

    assert not any("Фон за неделю" in m for m in tg.sent)


def test_weekly_failure_does_not_break_daily(state):
    """Недельный отчёт — дополнение; его падение не должно ронять прогон."""
    monday = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)

    class HalfBroken(FakeCrashlyticsWithVersions):
        def top_issues(self, start, end, error_types, versions=None):
            self.calls.append((start, end, error_types, versions))
            if len(self.calls) == 1:
                return [row("a")]
            raise CrashlyticsError("недельный запрос упал")

    crash = HalfBroken(VERSIONS)
    tg = FakeTelegram()
    run_once(CFG, Deps(crashlytics=crash, state=state, telegram=tg), now=monday)

    assert state.last_success_at() == monday      # дневной прогон засчитан
    assert any("Новые падения" in m or "новых падений нет" in m for m in tg.sent)
    assert any("Недельный отчёт не собран" in m for m in tg.sent)


def test_empty_version_list_stops_the_run(state):
    """Пустой каталог версий обязан останавливать прогон, а не отключать фильтр.

    Без этой проверки запрос уходил бы без filter.version.displayNames: числа
    считались бы по всем версиям, сообщение выглядело бы здоровым, а record()
    необратимо записал бы старые issue в состояние.
    """
    tg = FakeTelegram()
    deps = Deps(crashlytics=FakeCrashlyticsWithVersions([]), state=state, telegram=tg)

    with pytest.raises(CrashlyticsError):
        run_once(CFG, deps, now=NOW)

    assert state.last_success_at() is None
    assert tg.sent, "об отказе обязаны сообщить в канал"


def test_list_versions_failure_stops_the_run(state):
    class Broken(FakeCrashlyticsWithVersions):
        def list_versions(self, start, end):
            raise CrashlyticsError("HTTP 500")

    tg = FakeTelegram()
    deps = Deps(crashlytics=Broken(VERSIONS), state=state, telegram=tg)

    with pytest.raises(CrashlyticsError):
        run_once(CFG, deps, now=NOW)

    assert state.last_success_at() is None
    assert tg.sent


def test_bootstrap_is_version_filtered(state):
    crash = FakeCrashlyticsWithVersions(VERSIONS, [[row("a")]])
    deps = Deps(crashlytics=crash, state=state, telegram=FakeTelegram())

    run_once(CFG, deps, now=NOW, bootstrap=True)

    _, _, _, versions = crash.calls[0]
    assert versions == [
        "5.15.0 (2026082001)",
        "5.14.0 (2026072901)",
        "5.13.1 (2026063001)",
    ]


def test_weekly_due_on_the_configured_weekday_when_never_sent():
    monday = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    assert _weekly_due(None, monday, 0) is True


def test_weekly_not_due_on_other_days_when_never_sent():
    tuesday = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    assert _weekly_due(None, tuesday, 0) is False


def test_weekly_not_due_again_the_same_day():
    """Рестарт в понедельник не должен слать отчёт дважды."""
    monday = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    sent = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)
    assert _weekly_due(sent, monday, 0) is False


def test_weekly_due_after_a_missed_week_even_on_another_day():
    """Пропущенный понедельник не теряется: неделя прошла — шлём."""
    sent = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
    tuesday = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    assert _weekly_due(sent, tuesday, 0) is True




def test_catch_up_send_does_not_move_the_anchor_day():
    """Догоняющая отправка во вторник не должна утащить якорь на вторник.

    Следующий плановый понедельник наступает через шесть дней, а не семь,
    поэтому условие «прошла неделя» его бы пропустило — и дальше отчёт
    навсегда уезжал бы на вторник, потом на среду и так далее.
    """
    caught_up_tuesday = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    next_monday = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
    assert (next_monday - caught_up_tuesday).days == 6
    assert _weekly_due(caught_up_tuesday, next_monday, 0) is True


def test_weekly_send_is_recorded_in_state(state):
    """Без записи факта отправки рестарт в понедельник шлёт отчёт дважды."""
    monday = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    crash = FakeCrashlyticsWithVersions(VERSIONS, [[row("a")], [row("a")], []])

    run_once(CFG, Deps(crashlytics=crash, state=state, telegram=FakeTelegram()), now=monday)

    assert state.last_weekly_at() == monday

    # Второй прогон в тот же понедельник — уже без отчёта.
    crash2 = FakeCrashlyticsWithVersions(VERSIONS, [[row("b")], [row("b")], []])
    tg2 = FakeTelegram()
    run_once(CFG, Deps(crashlytics=crash2, state=state, telegram=tg2),
             now=monday.replace(hour=11))

    assert not any("Фон за неделю" in m for m in tg2.sent)


def test_startup_message_is_a_rich_table():
    text = _startup_text(CFG, zone="Europe/Moscow", now=NOW, last_success=None)
    assert "# 🚀 Example — контейнер запущен" in text
    assert "| 🕐 Зона | Europe/Moscow |" in text
    assert "| ⏱ Расписание |" in text
    assert "ещё не было" in text


def test_failures_go_through_send_plain(state):
    """Сообщение об отказе обязано идти старым методом, а не rich."""
    calls = {"rich": 0, "plain": 0}

    class TG:
        def send(self, messages):
            calls["rich"] += 1

        def send_plain(self, messages):
            calls["plain"] += 1

    class Broken(FakeCrashlyticsWithVersions):
        def list_versions(self, start, end):
            raise CrashlyticsError("HTTP 500")

    deps = Deps(crashlytics=Broken(VERSIONS), state=state, telegram=TG())
    with pytest.raises(CrashlyticsError):
        run_once(CFG, deps, now=NOW)

    assert calls["plain"] == 1, "об отказе сообщаем через sendMessage"
    assert calls["rich"] == 0


def test_main_reports_unreadable_key_to_channel_at_startup(monkeypatch, tmp_path):
    """Ключ читается при сборке зависимостей: не смонтированный файл должен
    прийти в канал сразу при старте, а не в плановый час прогона.
    """
    missing = str(tmp_path / "google-service-account.json")
    monkeypatch.setenv("CRASHLYTICS_PROJECT", "p")
    monkeypatch.setenv("CRASHLYTICS_APP_ID", "a")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", missing)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bt")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("STATE_PATH", ":memory:")

    sent = []

    class FakeTG:
        def __init__(self, *a, **kw):
            pass

        def send(self, messages):
            sent.extend(messages)

        def send_plain(self, messages):
            sent.extend(messages)

    monkeypatch.setattr("crashdigest.main.Telegram", FakeTG)

    import crashdigest.main as main_mod

    code = main_mod.main(["--once"])

    assert code == 1
    assert sent and missing in sent[0]
