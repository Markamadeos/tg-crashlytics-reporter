"""Клиент Crashlytics v1alpha: отчёт topIssues за интервал."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import requests

from crashdigest.versions import AppVersion

API_BASE = "https://firebasecrashlytics.googleapis.com/v1alpha"
PAGE_SIZE = 50
# topVersions молча обрезает выдачу по запрошенному pageSize и НЕ отдаёт
# nextPageToken, когда это делает. Значит единственный признак обрезки —
# что вернулось ровно столько, сколько просили. Берём с большим запасом.
VERSIONS_PAGE_SIZE = 1000


class CrashlyticsError(Exception):
    """Запрос к Crashlytics не удался."""


class SchemaError(CrashlyticsError):
    """Ответ пришёл в неожиданной форме — вероятно, v1alpha изменился."""


@dataclass(frozen=True)
class IssueRow:
    issue_id: str
    title: str
    subtitle: str
    error_type: str
    state: str
    last_seen_version: str
    first_seen_version: str
    events_count: int
    impacted_users: int


def _as_int(value, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"поле {field} не число: {value!r}") from exc


def parse_report(payload: dict) -> list[IssueRow]:
    """Превращает сырой ответ отчёта в список строк. Чистая функция."""
    rows: list[IssueRow] = []
    for index, group in enumerate(payload.get("groups") or []):
        if not isinstance(group, dict):
            raise SchemaError(f"в группе {index} структура группы неверна: не dict")

        issue = group.get("issue")
        if not isinstance(issue, dict):
            raise SchemaError(f"в группе {index} нет ключа 'issue'")

        metrics = group.get("metrics")
        if not isinstance(metrics, (list, tuple)):
            raise SchemaError(
                f"в группе {index} 'metrics' неверна: должна быть последовательность"
            )
        if not metrics:
            raise SchemaError(
                f"в группе {index} нет 'metrics' — нечего показать по объёму"
            )
        if len(metrics) > 1:
            # Код ниже читает только metrics[0], молча предполагая ровно одно
            # ведро. Недельный отчёт — первый потребитель семидневного окна:
            # если бакетинг когда-нибудь изменится, «неделя» тихо станет
            # «первым днём недели» вместо явной ошибки.
            raise SchemaError(
                f"в группе {index} 'metrics' содержит {len(metrics)} элементов, "
                "ожидался один — бакетинг отчёта изменился"
            )
        first = metrics[0]
        if not isinstance(first, dict):
            raise SchemaError(f"в группе {index} элемент metrics неверен: не dict")

        issue_id = issue.get("id")
        if not issue_id:
            raise SchemaError(f"в группе {index} у issue нет 'id'")

        rows.append(
            IssueRow(
                issue_id=issue_id,
                title=issue.get("title") or "",
                subtitle=issue.get("subtitle") or "",
                error_type=issue.get("errorType") or "UNKNOWN",
                state=issue.get("state") or "UNKNOWN",
                last_seen_version=issue.get("lastSeenVersion") or "?",
                first_seen_version=issue.get("firstSeenVersion") or "?",
                events_count=_as_int(first.get("eventsCount"), "eventsCount"),
                impacted_users=_as_int(
                    first.get("impactedUsersCount"), "impactedUsersCount"
                ),
            )
        )
    return rows


def parse_versions(payload: dict) -> list[AppVersion]:
    """Превращает ответ topVersions в список версий. Чистая функция."""
    rows: list[AppVersion] = []
    for index, group in enumerate(payload.get("groups") or []):
        if not isinstance(group, dict):
            raise SchemaError(f"в группе {index} структура группы неверна: не dict")

        version = group.get("version")
        if not isinstance(version, dict):
            raise SchemaError(f"в группе {index} нет ключа 'version' или он не объект")

        display_version = version.get("displayVersion")
        build_version = version.get("buildVersion")
        if not display_version or not build_version:
            raise SchemaError(
                f"в группе {index} у version нет 'displayVersion' или 'buildVersion'"
            )

        rows.append(
            AppVersion(
                display_version=display_version,
                build_version=build_version,
                display_name=version.get("displayName")
                or f"{display_version} ({build_version})",
            )
        )
    return rows


def _rfc3339(value: datetime, name: str) -> str:
    if value.tzinfo is None:
        raise ValueError(f"{name} должен быть timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class CrashlyticsClient:
    """Тонкая обёртка над reports.get topIssues."""

    def __init__(self, project: str, app_id: str, token_provider, session=None):
        self._project = project
        self._app_id = app_id
        self._tokens = token_provider
        self._session = session or requests.Session()

    def top_issues(
        self,
        start: datetime,
        end: datetime,
        error_types: Sequence[str],
        versions: Sequence[str] | None = None,
    ) -> list[IssueRow]:
        url = (
            f"{API_BASE}/projects/{self._project}"
            f"/apps/{self._app_id}/reports/topIssues"
        )
        params = {
            "filter.interval.startTime": _rfc3339(start, "start"),
            "filter.interval.endTime": _rfc3339(end, "end"),
            "filter.issue.errorTypes": list(error_types),
            "pageSize": str(PAGE_SIZE),
        }
        if versions:
            params["filter.version.displayNames"] = list(versions)
        headers = {"Authorization": f"Bearer {self._tokens.token()}"}

        rows: list[IssueRow] = []
        while True:
            response = self._session.get(
                url, params=params, headers=headers, timeout=90
            )
            if response.status_code != 200:
                raise CrashlyticsError(
                    f"Crashlytics вернул HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            payload = response.json()
            rows.extend(parse_report(payload))

            token = payload.get("nextPageToken")
            if not token:
                return rows
            params = {**params, "pageToken": token}

    def list_versions(self, start: datetime, end: datetime) -> list[AppVersion]:
        """Все известные версии приложения за интервал, одной страницей."""
        url = (
            f"{API_BASE}/projects/{self._project}"
            f"/apps/{self._app_id}/reports/topVersions"
        )
        params = {
            "filter.interval.startTime": _rfc3339(start, "start"),
            "filter.interval.endTime": _rfc3339(end, "end"),
            "pageSize": str(VERSIONS_PAGE_SIZE),
        }
        headers = {"Authorization": f"Bearer {self._tokens.token()}"}

        rows: list[AppVersion] = []
        while True:
            response = self._session.get(url, params=params, headers=headers, timeout=90)
            if response.status_code != 200:
                raise CrashlyticsError(
                    f"Crashlytics вернул HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            payload = response.json()
            page = parse_versions(payload)
            rows.extend(page)

            token = payload.get("nextPageToken")
            if not token:
                # Проверяем размер именно ЭТОЙ страницы, а не накопленных
                # rows: при реальной пагинации несколько полных страниц плюс
                # неполный хвост дали бы rows >= VERSIONS_PAGE_SIZE и ложную
                # тревогу, убивающую дневной прогон на пустом месте.
                if len(page) >= VERSIONS_PAGE_SIZE:
                    raise CrashlyticsError(
                        f"topVersions вернул страницу из {len(page)} версий — столько "
                        f"же, сколько запрошено, значит список может быть обрезан. "
                        f"Тихо усечённый каталог даёт неверные «последние версии»: "
                        f"поднимите VERSIONS_PAGE_SIZE."
                    )
                return rows
            params = {**params, "pageToken": token}
