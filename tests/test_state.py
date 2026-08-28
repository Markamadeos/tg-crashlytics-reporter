from datetime import datetime, timedelta, timezone

import pytest

from crashdigest.crashlytics import IssueRow
from crashdigest.state import DEFAULT_WINDOW, WINDOW_CAP, State

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)


def row(issue_id, events=1):
    return IssueRow(
        issue_id=issue_id,
        title="t",
        subtitle="s",
        error_type="FATAL",
        state="OPEN",
        last_seen_version="5.0.0",
        first_seen_version="5.0.0",
        events_count=events,
        impacted_users=1,
    )


@pytest.fixture
def state(tmp_path):
    st = State(str(tmp_path / "state.db"))
    yield st
    st.close()


def test_first_window_defaults_to_one_day(state):
    start, end, truncated = state.window(NOW)
    assert end == NOW
    assert start == NOW - DEFAULT_WINDOW
    assert truncated is False


def test_window_starts_at_last_success(state):
    state.mark_success(NOW - timedelta(hours=30))
    start, end, truncated = state.window(NOW)
    assert start == NOW - timedelta(hours=30)
    assert truncated is False


def test_window_capped_at_ninety_days(state):
    state.mark_success(NOW - timedelta(days=200))
    start, end, truncated = state.window(NOW)
    assert start == NOW - WINDOW_CAP
    assert truncated is True


def test_window_clamps_future_last_success_to_now(state):
    """Часы NAS сбросились до NTP-синхронизации: last_success_at в будущем."""
    state.mark_success(NOW + timedelta(hours=5))
    start, end, truncated = state.window(NOW)
    assert start == NOW
    assert end == NOW
    assert truncated is False


def test_last_success_at_returns_none_when_never_succeeded(state):
    assert state.last_success_at() is None


def test_last_success_at_returns_stored_timestamp(state):
    state.mark_success(NOW)
    assert state.last_success_at() == NOW


def test_window_rejects_naive_now(state):
    with pytest.raises(ValueError):
        state.window(datetime(2026, 8, 26, 10, 0))


def test_diff_new_returns_everything_on_empty_state(state):
    rows = [row("a"), row("b")]
    assert state.diff_new(rows) == rows


def test_recorded_issues_are_no_longer_new(state):
    state.record([row("a")], NOW)
    assert state.diff_new([row("a"), row("b")]) == [row("b")]


def test_issue_that_disappeared_and_returned_is_not_new(state):
    state.record([row("a")], NOW)
    assert state.diff_new([]) == []
    assert state.diff_new([row("a")]) == []


def test_record_is_idempotent_and_updates_last_seen(state):
    state.record([row("a")], NOW)
    first_seen, _ = state.seen_at("a")
    state.record([row("a")], NOW + timedelta(days=1))

    assert state.known_count() == 1
    again_first, last_seen = state.seen_at("a")
    assert again_first == first_seen          # новизна не сбрасывается
    assert last_seen > first_seen             # последняя встреча подвинулась


def test_state_survives_reopen(tmp_path):
    path = str(tmp_path / "state.db")
    first = State(path)
    first.record([row("a")], NOW)
    first.mark_success(NOW)
    first.close()

    second = State(path)
    assert second.diff_new([row("a")]) == []
    assert second.window(NOW + timedelta(days=1))[0] == NOW
    second.close()
