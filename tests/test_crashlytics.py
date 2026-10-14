import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crashdigest.crashlytics import (
    CONTEXT_TAIL,
    CrashlyticsClient,
    CrashlyticsError,
    IssueRow,
    SchemaError,
    parse_event,
    parse_report,
    parse_versions,
)
from crashdigest.versions import AppVersion

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_report_maps_fields():
    rows = parse_report(load_fixture("top_issues.json"))
    assert len(rows) == 2
    first = rows[0]
    assert isinstance(first, IssueRow)
    assert first.issue_id == "79904dab067716604c9a61a4c9af9c19"
    assert first.error_type == "FATAL"
    assert first.last_seen_version == "5.7.0"
    assert first.events_count == 73          # строка из API стала int
    assert first.impacted_users == 14


def test_parse_report_handles_empty():
    assert parse_report(load_fixture("top_issues_empty.json")) == []


def test_parse_report_rejects_missing_issue_key():
    with pytest.raises(SchemaError) as exc:
        parse_report({"groups": [{"metrics": [{"eventsCount": "1"}]}]})
    assert "issue" in str(exc.value)


def test_parse_report_rejects_missing_metrics():
    payload = {"groups": [{"issue": {"id": "x", "errorType": "FATAL"}}]}
    with pytest.raises(SchemaError):
        parse_report(payload)


def test_parse_report_rejects_non_dict_group():
    with pytest.raises(SchemaError) as exc:
        parse_report({"groups": ["oops"]})
    assert "группе" in str(exc.value)


def test_parse_report_rejects_non_list_metrics():
    with pytest.raises(SchemaError) as exc:
        parse_report({"groups": [{"issue": {"id": "x"}, "metrics": "oops"}]})
    assert "metrics" in str(exc.value) and "последовательность" in str(exc.value)


def test_parse_report_rejects_non_dict_metrics_element():
    with pytest.raises(SchemaError) as exc:
        parse_report({"groups": [{"issue": {"id": "x"}, "metrics": [None]}]})
    assert "metrics" in str(exc.value) and "dict" in str(exc.value)


def test_parse_report_rejects_multiple_metrics_buckets():
    with pytest.raises(SchemaError) as exc:
        parse_report({
            "groups": [{
                "issue": {"id": "x", "errorType": "FATAL"},
                "metrics": [
                    {"eventsCount": "1", "impactedUsersCount": "1"},
                    {"eventsCount": "2", "impactedUsersCount": "1"},
                ]
            }]
        })
    assert "metrics" in str(exc.value) and "2" in str(exc.value)


def test_parse_report_captures_first_seen_version():
    rows = parse_report(load_fixture("top_issues.json"))
    assert rows[0].first_seen_version == "5.7.0"


def test_missing_first_seen_version_becomes_placeholder():
    payload = {"groups": [{"issue": {"id": "x", "errorType": "FATAL"},
                           "metrics": [{"eventsCount": "1", "impactedUsersCount": "1"}]}]}
    assert parse_report(payload)[0].first_seen_version == "?"


def test_parse_versions_maps_fields():
    rows = parse_versions(load_fixture("top_versions.json"))
    assert len(rows) == 4
    assert rows[0] == AppVersion(
        display_version="5.15.0",
        build_version="2026082001",
        display_name="5.15.0 (2026082001)",
    )


def test_parse_versions_handles_empty():
    assert parse_versions({"groups": []}) == []


def test_parse_versions_rejects_group_without_version():
    with pytest.raises(SchemaError) as exc:
        parse_versions({"groups": [{"metrics": [{"eventsCount": "1"}]}]})
    assert "version" in str(exc.value)


class FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.responses.pop(0)


class FakeTokens:
    def token(self):
        return "at-1"


def test_client_builds_request_and_returns_rows():
    session = FakeSession(FakeResponse(200, load_fixture("top_issues.json")))
    client = CrashlyticsClient("example-prod", "app-1", FakeTokens(), session=session)
    start = datetime(2026, 8, 25, 17, 40, 56, tzinfo=timezone.utc)
    end = datetime(2026, 8, 26, 17, 40, 56, tzinfo=timezone.utc)

    rows = client.top_issues(start, end, ("FATAL", "ANR"))

    assert len(rows) == 2
    url, params, headers = session.calls[0]
    assert url.endswith("/projects/example-prod/apps/app-1/reports/topIssues")
    assert params["filter.interval.startTime"] == "2026-08-25T17:40:56Z"
    assert params["filter.interval.endTime"] == "2026-08-26T17:40:56Z"
    assert params["filter.issue.errorTypes"] == ["FATAL", "ANR"]
    assert headers["Authorization"] == "Bearer at-1"


def test_client_follows_pagination():
    page1 = load_fixture("top_issues.json") | {"nextPageToken": "tok"}
    page2 = load_fixture("top_issues_empty.json")
    session = FakeSession(FakeResponse(200, page1), FakeResponse(200, page2))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)

    rows = client.top_issues(
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        ("FATAL",),
    )

    assert len(rows) == 2
    assert session.calls[1][1]["pageToken"] == "tok"


def test_client_raises_on_http_error_with_diagnostics():
    session = FakeSession(FakeResponse(403, {}, text="Caller does not have permission"))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)
    with pytest.raises(CrashlyticsError) as exc:
        client.top_issues(
            datetime(2026, 8, 25, tzinfo=timezone.utc),
            datetime(2026, 8, 26, tzinfo=timezone.utc),
            ("FATAL",),
        )
    assert "403" in str(exc.value)
    assert "permission" in str(exc.value)


def test_client_rejects_naive_datetimes():
    client = CrashlyticsClient("p", "a", FakeTokens(), session=FakeSession())
    with pytest.raises(ValueError):
        client.top_issues(datetime(2026, 8, 25), datetime(2026, 8, 26), ("FATAL",))


def test_list_versions_calls_top_versions_report():
    session = FakeSession(FakeResponse(200, load_fixture("top_versions.json")))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)

    rows = client.list_versions(
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert len(rows) == 4
    url, params, _ = session.calls[0]
    assert url.endswith("/reports/topVersions")
    assert params["filter.interval.startTime"] == "2026-08-27T00:00:00Z"


def test_list_versions_raises_when_response_may_be_truncated(monkeypatch):
    """API не сигналит об обрезке — единственный признак это ровно pageSize строк."""
    monkeypatch.setattr("crashdigest.crashlytics.VERSIONS_PAGE_SIZE", 2)
    payload = {
        "groups": [
            {
                "version": {
                    "displayVersion": f"1.{i}.0",
                    "buildVersion": f"{100 + i}",
                    "displayName": f"1.{i}.0 ({100 + i})",
                },
                "metrics": [{"eventsCount": "1"}],
            }
            for i in range(2)
        ]
    }
    session = FakeSession(FakeResponse(200, payload))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)

    with pytest.raises(CrashlyticsError) as exc:
        client.list_versions(
            datetime(2026, 8, 27, tzinfo=timezone.utc),
            datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
    assert "обрезан" in str(exc.value)


def test_paginated_catalogue_is_not_mistaken_for_truncation(monkeypatch):
    """Обрезку выдаёт размер ПОСЛЕДНЕЙ страницы, а не сумма накопленного.

    Полная страница плюс неполный хвост — это нормальная пагинация. Если
    мерить накопленные строки, такой ответ поднимет ложную тревогу и убьёт
    дневной прогон на пустом месте.
    """
    monkeypatch.setattr("crashdigest.crashlytics.VERSIONS_PAGE_SIZE", 2)

    def group(i):
        return {
            "version": {
                "displayVersion": f"1.{i}.0",
                "buildVersion": f"{100 + i}",
                "displayName": f"1.{i}.0 ({100 + i})",
            },
            "metrics": [{"eventsCount": "1"}],
        }

    full_page = {"groups": [group(0), group(1)], "nextPageToken": "next"}
    tail_page = {"groups": [group(2)]}
    session = FakeSession(FakeResponse(200, full_page), FakeResponse(200, tail_page))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)

    rows = client.list_versions(
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert len(rows) == 3, "три версии за две страницы, тревоги быть не должно"


def test_list_versions_returns_normally_below_the_page_size(monkeypatch):
    monkeypatch.setattr("crashdigest.crashlytics.VERSIONS_PAGE_SIZE", 10)
    session = FakeSession(FakeResponse(200, load_fixture("top_versions.json")))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)

    rows = client.list_versions(
        datetime(2026, 8, 27, tzinfo=timezone.utc),
        datetime(2026, 8, 28, tzinfo=timezone.utc),
    )
    assert len(rows) == 4


def test_top_issues_passes_versions_as_repeated_param():
    session = FakeSession(FakeResponse(200, load_fixture("top_issues.json")))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)

    client.top_issues(
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        ("FATAL",),
        versions=["5.15.0 (2026082001)", "5.14.0 (2026072901)"],
    )

    _, params, _ = session.calls[0]
    assert params["filter.version.displayNames"] == [
        "5.15.0 (2026082001)",
        "5.14.0 (2026072901)",
    ]


def test_top_issues_omits_version_filter_when_not_given():
    session = FakeSession(FakeResponse(200, load_fixture("top_issues.json")))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)

    client.top_issues(
        datetime(2026, 8, 25, tzinfo=timezone.utc),
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        ("FATAL",),
    )

    _, params, _ = session.calls[0]
    assert "filter.version.displayNames" not in params


# --- сэмпл события: стектрейс ----------------------------------------------

ALL_CONTEXT = ("breadcrumbs", "logs", "custom_keys", "user")


def test_parse_event_maps_stack_and_metadata():
    event = parse_event(load_fixture("event.json"))

    assert event.platform == "ANDROID"
    assert event.app_version == "5.15.0 (515)"
    assert event.device == "Redmi 9"
    assert event.operating_system == "Android (11)"
    assert event.memory == "свободно 120 МБ, использовано 1800 МБ"
    assert event.process_state == "FOREGROUND"

    assert len(event.exceptions) == 1
    exception = event.exceptions[0]
    assert exception.type == "java.lang.IllegalStateException"
    assert exception.message == "FragmentManager is already executing transactions"
    assert exception.blamed is True
    assert len(exception.frames) == 5

    blamed = [f for f in exception.frames if f.blamed]
    assert len(blamed) == 1
    assert blamed[0].owner == "DEVELOPER"
    assert blamed[0].where == (
        "com.example.app.profile.ProfileFragment.onResume (ProfileFragment.kt:142)"
    )


def test_parse_event_keeps_only_blamed_or_crashed_threads():
    """Невиноватых потоков в событии десятки, и разбору они не нужны ни разу.
    Тащить их через весь модуль только чтобы выбросить — незачем."""
    event = parse_event(load_fixture("event.json"))
    assert [t.name for t in event.threads] == ["main"]


def test_parse_event_collects_requested_context():
    event = parse_event(load_fixture("event.json"),
                        context=("breadcrumbs", "custom_keys", "user"))
    assert event.breadcrumbs == ("screen_view", "select_item", "screen_view")
    assert event.custom_keys == (("auth_state", "logged_in"),
                                 ("experiment", "profile_v2"))
    assert event.user == "u-90210"


def test_parse_event_drops_context_blocks_not_allowed():
    """Единственный рубеж приватности: readMask живой API не принимает,
    событие приезжает целиком, и отбор делается здесь. Собранный CrashEvent
    не должен хранить того, чему нельзя уехать в модель.
    """
    event = parse_event(load_fixture("event.json"),
                        context=("breadcrumbs", "custom_keys", "user"))
    assert event.logs == (), "логи не разрешены, но остались в событии"

    nothing = parse_event(load_fixture("event.json"))
    assert nothing.breadcrumbs == ()
    assert nothing.custom_keys == ()
    assert nothing.user == ""
    assert nothing.logs == ()
    # Стектрейс и метаданные при этом на месте: они не контекст.
    assert nothing.exceptions and nothing.device


def test_parse_event_keeps_logs_when_explicitly_allowed():
    event = parse_event(
        {"events": [{"logs": [{"message": "шаг 1"}, {"message": "шаг 2"}]}]},
        context=("logs",),
    )
    assert event.logs == ("шаг 1", "шаг 2")


def test_parse_event_returns_none_without_events():
    assert parse_event({}) is None
    assert parse_event({"events": []}) is None


def test_parse_event_tolerates_missing_fields():
    """НАМЕРЕННО снисходительна, в отличие от строгой parse_report: разбор —
    необязательная надстройка, и пропавшее поле обязано обеднить его, а не
    уронить весь дневной прогон.
    """
    event = parse_event({"events": [{"platform": "ANDROID"}]})
    assert event.platform == "ANDROID"
    assert event.exceptions == ()
    assert event.threads == ()
    assert event.blame_frame is None
    assert event.device == ""
    assert event.memory == ""


def test_parse_event_tolerates_wrong_types():
    event = parse_event({"events": [{
        "exceptions": "не список",
        "device": 42,
        "memory": {"free": "дырка"},
        "threads": [None, {"blamed": True, "frames": None}],
        "customKeys": "не объект",
    }]}, context=ALL_CONTEXT)
    assert event.exceptions == ()
    assert event.device == ""
    assert event.memory == ""
    assert len(event.threads) == 1 and event.threads[0].frames == ()
    assert event.custom_keys == ()


def test_parse_event_trims_long_context_tails():
    payload = {"events": [{
        "breadcrumbs": [{"title": f"e{i}"} for i in range(100)],
        "logs": [{"message": f"l{i}"} for i in range(100)],
    }]}
    event = parse_event(payload, context=ALL_CONTEXT)
    assert len(event.breadcrumbs) == CONTEXT_TAIL
    assert event.breadcrumbs[-1] == "e99"      # хвост, а не голова
    assert len(event.logs) == CONTEXT_TAIL


def test_sample_event_requests_single_newest_event():
    session = FakeSession(FakeResponse(200, load_fixture("event.json")))
    client = CrashlyticsClient("example-prod", "app-1", FakeTokens(), session=session)
    start = datetime(2026, 10, 13, tzinfo=timezone.utc)
    end = datetime(2026, 10, 14, tzinfo=timezone.utc)

    event = client.sample_event("iss-1", start, end, context=("breadcrumbs",))

    assert event is not None
    url, params, headers = session.calls[0]
    assert url.endswith("/projects/example-prod/apps/app-1/events")
    assert params["filter.issue.id"] == "iss-1"
    assert params["pageSize"] == "1"
    assert params["filter.interval.startTime"] == "2026-10-13T00:00:00Z"
    assert headers["Authorization"] == "Bearer at-1"
    # readMask не передаём намеренно: живой API отвечает 400 на любое его
    # значение, хотя discovery-документ его описывает.
    assert "readMask" not in params


def test_sample_event_applies_context_to_the_parsed_event():
    session = FakeSession(FakeResponse(200, load_fixture("event.json")))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)
    event = client.sample_event(
        "iss-1", datetime(2026, 10, 13, tzinfo=timezone.utc),
        datetime(2026, 10, 14, tzinfo=timezone.utc), context=("user",),
    )
    assert event.user == "u-90210"
    assert event.breadcrumbs == ()


def test_sample_event_returns_none_when_window_has_no_events():
    session = FakeSession(FakeResponse(200, {"events": []}))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)
    assert client.sample_event(
        "iss-1", datetime(2026, 10, 13, tzinfo=timezone.utc),
        datetime(2026, 10, 14, tzinfo=timezone.utc),
    ) is None


def test_sample_event_explains_insufficient_role_on_403():
    """Дайджесту хватает reports, а events.list — отдельное разрешение.
    Голое «403» выглядело бы как отозванный ключ и послало бы чинить не то."""
    session = FakeSession(FakeResponse(403, {}, text="permission denied"))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)
    with pytest.raises(CrashlyticsError) as exc:
        client.sample_event(
            "iss-1", datetime(2026, 10, 13, tzinfo=timezone.utc),
            datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
    assert "роли сервисного аккаунта не хватает" in str(exc.value)
    assert "Дайджест при этом работает" in str(exc.value)


def test_sample_event_reports_other_http_errors():
    session = FakeSession(FakeResponse(500, {}, text="boom"))
    client = CrashlyticsClient("p", "a", FakeTokens(), session=session)
    with pytest.raises(CrashlyticsError) as exc:
        client.sample_event(
            "iss-1", datetime(2026, 10, 13, tzinfo=timezone.utc),
            datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
    assert "HTTP 500" in str(exc.value)
