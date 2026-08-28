"""Загрузка и валидация настроек из окружения."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from croniter import croniter

REQUIRED = (
    "CRASHLYTICS_PROJECT",
    "CRASHLYTICS_APP_ID",
    "GOOGLE_REFRESH_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


class ConfigError(Exception):
    """Настройки заданы неполно или неверно."""


def _int_setting(env: Mapping[str, str], key: str, default: int,
                 low: int, high: int) -> int:
    """Целочисленная настройка с проверкой диапазона.

    Кривое значение обязано падать на загрузке конфига, а не через сутки
    посреди прогона: контейнер с restart: unless-stopped уйдёт в цикл
    перезапусков, а канал будет молчать.
    """
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} должен быть целым числом, получено {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{key} должен быть в диапазоне {low}..{high}, получено {value}")
    return value


@dataclass(frozen=True)
class Config:
    project: str
    app_id: str
    refresh_token: str
    client_id: str
    client_secret: str
    bot_token: str
    chat_id: str
    proxy: str | None
    error_types: tuple[str, ...]
    schedule: str
    state_path: str
    app_name: str
    version_window: int
    weekly_top: int
    weekly_weekday: int


def load(env: Mapping[str, str]) -> Config:
    missing = [key for key in REQUIRED if not (env.get(key) or "").strip()]
    if missing:
        raise ConfigError(
            "не заданы обязательные переменные окружения: " + ", ".join(missing)
        )

    raw_types = env.get("ERROR_TYPES") or "FATAL"
    error_types = tuple(
        part.strip().upper() for part in raw_types.split(",") if part.strip()
    )
    if not error_types:
        raise ConfigError("ERROR_TYPES не содержит ни одного значения")

    proxy = (env.get("TELEGRAM_PROXY") or "").strip() or None

    # Пробел — истинное значение для `or`, поэтому дефолт срабатывает только
    # если сначала обрезать пробелы, а уже потом проверять пустоту: иначе
    # SCHEDULE=" " или STATE_PATH=" " молча превращаются в "" вместо дефолта.
    schedule = (env.get("SCHEDULE") or "").strip() or "0 10 * * *"
    if not croniter.is_valid(schedule):
        raise ConfigError(f"SCHEDULE не является корректным cron-выражением: {schedule!r}")

    state_path = (env.get("STATE_PATH") or "").strip() or "/data/state.db"
    app_name = (env.get("CRASHLYTICS_APP_NAME") or "").strip() or "App"

    version_window = _int_setting(env, "VERSION_WINDOW", 3, 1, 50)
    weekly_top = _int_setting(env, "WEEKLY_TOP", 5, 1, 30)
    weekly_weekday = _int_setting(env, "WEEKLY_WEEKDAY", 0, 0, 6)

    return Config(
        project=env["CRASHLYTICS_PROJECT"].strip(),
        app_id=env["CRASHLYTICS_APP_ID"].strip(),
        refresh_token=env["GOOGLE_REFRESH_TOKEN"].strip(),
        client_id=env["GOOGLE_CLIENT_ID"].strip(),
        client_secret=env["GOOGLE_CLIENT_SECRET"].strip(),
        bot_token=env["TELEGRAM_BOT_TOKEN"].strip(),
        chat_id=env["TELEGRAM_CHAT_ID"].strip(),
        proxy=proxy,
        error_types=error_types,
        schedule=schedule,
        state_path=state_path,
        app_name=app_name,
        version_window=version_window,
        weekly_top=weekly_top,
        weekly_weekday=weekly_weekday,
    )
