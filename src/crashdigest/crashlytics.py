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

    def sample_event(self, issue_id: str, start: datetime, end: datetime,
                     *, context: Sequence[str] = ()) -> CrashEvent | None:
        """Самое свежее событие issue — ради стектрейса и обстановки.

        pageSize=1: разбору нужен один представитель, а не выборка. События
        отсортированы по убыванию времени, значит первое — последнее по времени,
        то есть снятое с самой свежей сборки из попавших в окно.
        """
        url = f"{API_BASE}/projects/{self._project}/apps/{self._app_id}/events"
        params = {
            "filter.issue.id": issue_id,
            "filter.interval.startTime": _rfc3339(start, "start"),
            "filter.interval.endTime": _rfc3339(end, "end"),
            "pageSize": "1",
        }
        headers = {"Authorization": f"Bearer {self._tokens.token()}"}

        response = self._session.get(url, params=params, headers=headers, timeout=90)
        if response.status_code == 403:
            # Дайджесту хватает роли Firebase Crashlytics Viewer, а events.list
            # — отдельное разрешение. Отличаем эту причину явно: иначе «403» на
            # ровном месте выглядит как отозванный ключ.
            raise CrashlyticsError(
                "Crashlytics вернул HTTP 403 на events.list — роли сервисного "
                "аккаунта не хватает на чтение событий (стектрейсов). "
                "Дайджест при этом работает: ему достаточно reports. "
                f"{response.text[:200]}"
            )
        if response.status_code != 200:
            raise CrashlyticsError(
                f"Crashlytics вернул HTTP {response.status_code} на events.list: "
                f"{response.text[:200]}"
            )
        return parse_event(response.json(), context=context)


# --- Сэмпл события: стектрейс и обстановка вокруг краша ---------------------

# ВНИМАНИЕ: параметра readMask у этого метода фактически нет. Discovery-документ
# v1alpha его описывает, но живой API отвечает 400 INVALID_ARGUMENT на ЛЮБОЕ
# его значение — проверено на проде: `exceptions`, `exceptions,blameFrame`,
# полная маска, snake_case, `*`. Без него тот же запрос отдаёт событие целиком.
# Поэтому событие всегда приезжает полностью, со всеми логами и крошками, а
# отбор того, что разрешено показывать модели, делает `parse_event` ниже —
# другого рубежа для него не осталось.
#
# Обязателен же здесь `filter.issue.id`: без него метод тоже отвечает 400.

# Сколько элементов хвоста контекста оставляем. Крошек и логов в сессии бывают
# сотни, а для «что делал юзер перед падением» хватает последних.
CONTEXT_TAIL = 15


@dataclass(frozen=True)
class EventFrame:
    symbol: str
    file: str
    line: str
    library: str
    owner: str
    blamed: bool

    @property
    def where(self) -> str:
        """`symbol (file:line)` — ровно то, что видно в консоли Crashlytics."""
        place = ":".join(part for part in (self.file, self.line) if part)
        return f"{self.symbol} ({place})" if place else self.symbol


@dataclass(frozen=True)
class EventException:
    type: str
    message: str
    frames: tuple[EventFrame, ...]
    blamed: bool
    nested: bool


@dataclass(frozen=True)
class EventThread:
    name: str
    signal: str
    crashed: bool
    blamed: bool
    frames: tuple[EventFrame, ...]


@dataclass(frozen=True)
class CrashEvent:
    event_time: str
    platform: str
    issue_title: str
    issue_subtitle: str
    exceptions: tuple[EventException, ...]
    threads: tuple[EventThread, ...]
    blame_frame: EventFrame | None
    app_version: str
    device: str
    operating_system: str
    os_version: str
    memory: str
    process_state: str
    orientation: str
    breadcrumbs: tuple[str, ...]
    logs: tuple[str, ...]
    custom_keys: tuple[tuple[str, str], ...]
    user: str


def _text(value) -> str:
    """Строка или пусто. Ни одно поле события не обязано присутствовать."""
    return value.strip() if isinstance(value, str) else ""


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value) -> list:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _parse_frames(raw) -> tuple[EventFrame, ...]:
    return tuple(
        EventFrame(
            symbol=_text(frame.get("symbol")),
            file=_text(frame.get("file")),
            line=_text(frame.get("line")),
            library=_text(frame.get("library")),
            owner=_text(frame.get("owner")) or "UNKNOWN",
            blamed=bool(frame.get("blamed")),
        )
        for frame in _items(raw)
    )


def _parse_memory(raw) -> str:
    """«свободно/использовано» в мегабайтах. Пусто, если чисел нет."""
    memory = _mapping(raw)
    parts = []
    for key, label in (("free", "свободно"), ("used", "использовано")):
        try:
            parts.append(f"{label} {int(memory[key]) // (1024 * 1024)} МБ")
        except (KeyError, TypeError, ValueError):
            continue
    return ", ".join(parts)


def parse_event(payload: dict, *, context: Sequence[str] = ()) -> CrashEvent | None:
    """Первое событие из ответа events.list. Чистая функция.

    НАМЕРЕННО снисходительна, в отличие от строгой `parse_report` рядом. У той
    строгость оправдана: молча испорченные числа в дайджесте хуже падения. Здесь
    наоборот — разбор это необязательная надстройка над уже доставленным постом,
    и пропавшее поле обязано обеднить разбор, а не уронить дневной прогон.

    `context` — имена блоков из CRASH_CONTEXT, которые разрешено оставить
    (`breadcrumbs`, `logs`, `custom_keys`, `user`). Неразрешённые выбрасываются
    ЗДЕСЬ, а не при сборке промпта: readMask живым API не поддерживается (см.
    комментарий выше), событие приезжает целиком, и чем раньше лишнее выпадет,
    тем меньше мест, где оно может всплыть — в логе, в тексте исключения, в
    repr датакласса. Собранный CrashEvent не хранит того, чего не должен.
    """
    events = _items(_mapping(payload).get("events"))
    if not events:
        return None
    event = events[0]
    allowed = set(context)

    exceptions = tuple(
        EventException(
            type=_text(item.get("type")),
            message=_text(item.get("exceptionMessage")) or _text(item.get("subtitle")),
            frames=_parse_frames(item.get("frames")),
            blamed=bool(item.get("blamed")),
            nested=bool(item.get("nested")),
        )
        for item in _items(event.get("exceptions"))
    )

    # Невиноватые потоки не сохраняем вовсе: их в событии десятки, разбору нужен
    # только упавший, и держать остальные ради «полноты» означало бы тащить
    # мегабайт фреймов через весь модуль до места, где они будут выброшены.
    threads = tuple(
        EventThread(
            name=_text(item.get("name")) or _text(item.get("title")),
            signal=_text(item.get("signal")),
            crashed=bool(item.get("crashed")),
            blamed=bool(item.get("blamed")),
            frames=_parse_frames(item.get("frames")),
        )
        for item in _items(event.get("threads"))
        if item.get("blamed") or item.get("crashed")
    )

    blame = _parse_frames([event.get("blameFrame")] if event.get("blameFrame") else [])

    return CrashEvent(
        event_time=_text(event.get("eventTime")),
        platform=_text(event.get("platform")),
        issue_title=_text(event.get("issueTitle")),
        issue_subtitle=_text(event.get("issueSubtitle")),
        exceptions=exceptions,
        threads=threads,
        blame_frame=blame[0] if blame else None,
        app_version=_text(_mapping(event.get("version")).get("displayName")),
        device=(
            _text(_mapping(event.get("device")).get("marketingName"))
            or _text(_mapping(event.get("device")).get("displayName"))
        ),
        operating_system=_text(_mapping(event.get("operatingSystem")).get("displayName")),
        # Отдельно от displayName: у боевого приложения тот приходит как
        # `Unknown (13)` — имя ОС Google не определил, а версию определил.
        # Имея версию и платформу по отдельности, можно не печатать `Unknown`.
        os_version=_text(_mapping(event.get("operatingSystem")).get("displayVersion")),
        memory=_parse_memory(event.get("memory")),
        process_state=_text(event.get("processState")),
        orientation=_text(event.get("appOrientation")),
        breadcrumbs=tuple(
            name for name in (
                _text(item.get("title")) for item in _items(event.get("breadcrumbs"))
            ) if name
        )[-CONTEXT_TAIL:] if "breadcrumbs" in allowed else (),
        logs=tuple(
            line for line in (
                _text(item.get("message")) for item in _items(event.get("logs"))
            ) if line
        )[-CONTEXT_TAIL:] if "logs" in allowed else (),
        custom_keys=tuple(
            (str(key), str(value))
            for key, value in _mapping(event.get("customKeys")).items()
        )[:CONTEXT_TAIL] if "custom_keys" in allowed else (),
        user=_text(_mapping(event.get("user")).get("id")) if "user" in allowed else "",
    )
