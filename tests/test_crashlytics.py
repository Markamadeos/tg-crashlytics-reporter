import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from crashdigest.crashlytics import (
    CrashlyticsClient,
    CrashlyticsError,
    IssueRow,
    SchemaError,
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
