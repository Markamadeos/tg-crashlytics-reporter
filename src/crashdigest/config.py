"""Загрузка и валидация настроек из окружения."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from croniter import croniter

REQUIRED = (
    "CRASHLYTICS_PROJECT",
    "CRASHLYTICS_APP_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


class ConfigError(Exception):
    """Настройки заданы неполно или неверно."""


# Блоки события, которые разрешено прикладывать к стектрейсу в промпте.
# `logs` в дефолт НЕ входит: строки Crashlytics.log() чаще всего и содержат
# пользовательские данные, а разбор уходит внешнему провайдеру через шлюз.
CONTEXT_BLOCKS = ("breadcrumbs", "logs", "custom_keys", "user")
DEFAULT_CONTEXT = "breadcrumbs,custom_keys,user"
DEFAULT_LLM_MODEL = "gemini-claude-opus-4-6-thinking"


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
    credentials_path: str
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
    # Разбор краша — необязательная надстройка над дайджестом, поэтому все её
    # поля имеют дефолты: без LLM_API_KEY конфиг собирается ровно как до неё.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = DEFAULT_LLM_MODEL
    llm_timeout: int = 120
    analyze_top: int = 5
    crash_context: tuple[str, ...] = ("breadcrumbs", "custom_keys", "user")

    @property
    def analysis_enabled(self) -> bool:
        """Разбор включён только при полном наборе: адрес шлюза, ключ и лимит.

        Предохранитель для прода: новый образ без LLM_API_KEY в окружении
        ведёт себя в точности как предыдущий, а не падает и не шлёт в канал
        сообщения об отказе шлюза.
        """
        return bool(self.llm_base_url and self.llm_api_key and self.analyze_top)


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

    # Адрес шлюза дефолта в коде не имеет намеренно: это адрес в чужой
    # локальной сети, а образ публичный. Значение по умолчанию живёт в
    # docker-compose.yml, где его видно рядом с описанием сервиса.
    llm_base_url = (env.get("LLM_BASE_URL") or "").strip().rstrip("/") or None
    if llm_base_url and not llm_base_url.startswith(("http://", "https://")):
        raise ConfigError(
            f"LLM_BASE_URL должен начинаться с http:// или https://, "
            f"получено {llm_base_url!r}"
        )
    llm_api_key = (env.get("LLM_API_KEY") or "").strip() or None
    llm_model = (env.get("LLM_MODEL") or "").strip() or DEFAULT_LLM_MODEL
    llm_timeout = _int_setting(env, "LLM_TIMEOUT", 120, 10, 600)
    analyze_top = _int_setting(env, "ANALYZE_TOP", 5, 0, 20)

    raw_context = env.get("CRASH_CONTEXT")
    if raw_context is None:
        raw_context = DEFAULT_CONTEXT
    crash_context = tuple(
        part.strip().lower() for part in raw_context.split(",") if part.strip()
    )
    unknown = [name for name in crash_context if name not in CONTEXT_BLOCKS]
    if unknown:
        # Опечатка в имени блока иначе молча обеднила бы промпт: разбор стал
        # бы хуже без единого признака, что настройка не применилась.
        raise ConfigError(
            "CRASH_CONTEXT содержит неизвестные блоки: " + ", ".join(unknown)
            + "; допустимы: " + ", ".join(CONTEXT_BLOCKS)
        )

    return Config(
        project=env["CRASHLYTICS_PROJECT"].strip(),
        app_id=env["CRASHLYTICS_APP_ID"].strip(),
        credentials_path=env["GOOGLE_APPLICATION_CREDENTIALS"].strip(),
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
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_timeout=llm_timeout,
        analyze_top=analyze_top,
        crash_context=crash_context,
    )
