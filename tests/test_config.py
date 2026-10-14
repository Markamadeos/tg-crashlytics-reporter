import pytest
from crashdigest.config import Config, ConfigError, load

BASE = {
    "CRASHLYTICS_PROJECT": "example-prod",
    "CRASHLYTICS_APP_ID": "1:111111111111:android:0000000000000000",
    "GOOGLE_APPLICATION_CREDENTIALS": "/secrets/google-service-account.json",
    "TELEGRAM_BOT_TOKEN": "bt",
    "TELEGRAM_CHAT_ID": "-1000000000000",
}


def test_loads_required_fields_and_applies_defaults():
    cfg = load(BASE)
    assert isinstance(cfg, Config)
    assert cfg.project == "example-prod"
    assert cfg.credentials_path == "/secrets/google-service-account.json"
    assert cfg.error_types == ("FATAL",)
    assert cfg.schedule == "0 10 * * *"
    assert cfg.app_name == "App"
    assert cfg.proxy is None


def test_error_types_parsed_and_uppercased():
    cfg = load({**BASE, "ERROR_TYPES": " fatal , anr "})
    assert cfg.error_types == ("FATAL", "ANR")


def test_blank_proxy_means_no_proxy():
    cfg = load({**BASE, "TELEGRAM_PROXY": "   "})
    assert cfg.proxy is None


def test_missing_keys_reported_all_at_once():
    with pytest.raises(ConfigError) as exc:
        load({"CRASHLYTICS_PROJECT": "p"})
    message = str(exc.value)
    assert "GOOGLE_APPLICATION_CREDENTIALS" in message
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "CRASHLYTICS_APP_ID" in message


def test_whitespace_only_schedule_falls_back_to_default():
    cfg = load({**BASE, "SCHEDULE": "   "})
    assert cfg.schedule == "0 10 * * *"


def test_whitespace_only_state_path_falls_back_to_default():
    cfg = load({**BASE, "STATE_PATH": "   "})
    assert cfg.state_path == "/data/state.db"


def test_whitespace_only_app_name_falls_back_to_default():
    cfg = load({**BASE, "CRASHLYTICS_APP_NAME": "   "})
    assert cfg.app_name == "App"


def test_invalid_schedule_raises_config_error():
    with pytest.raises(ConfigError) as exc:
        load({**BASE, "SCHEDULE": "not a cron"})
    assert "SCHEDULE" in str(exc.value)


def test_version_settings_defaults():
    cfg = load(BASE)
    assert cfg.version_window == 3
    assert cfg.weekly_top == 5
    assert cfg.weekly_weekday == 0  # понедельник


def test_version_settings_parsed():
    cfg = load({**BASE, "VERSION_WINDOW": "5", "WEEKLY_TOP": "10", "WEEKLY_WEEKDAY": "6"})
    assert cfg.version_window == 5
    assert cfg.weekly_top == 10
    assert cfg.weekly_weekday == 6


def test_version_window_must_be_positive():
    with pytest.raises(ConfigError) as exc:
        load({**BASE, "VERSION_WINDOW": "0"})
    assert "VERSION_WINDOW" in str(exc.value)


def test_non_numeric_setting_is_rejected():
    with pytest.raises(ConfigError) as exc:
        load({**BASE, "WEEKLY_TOP": "много"})
    assert "WEEKLY_TOP" in str(exc.value)


def test_weekday_out_of_range_is_rejected():
    with pytest.raises(ConfigError) as exc:
        load({**BASE, "WEEKLY_WEEKDAY": "7"})
    assert "WEEKLY_WEEKDAY" in str(exc.value)


# --- настройки разбора крашей ----------------------------------------------

def test_analysis_is_off_without_gateway_settings():
    """Предохранитель для прода: новый образ без LLM_API_KEY обязан вести себя
    в точности как предыдущий."""
    cfg = load(BASE)
    assert cfg.analysis_enabled is False
    assert cfg.llm_base_url is None
    assert cfg.llm_api_key is None


def test_analysis_defaults_when_enabled():
    cfg = load({**BASE, "LLM_BASE_URL": "http://gw:3000/v1",
                "LLM_API_KEY": "ag-key"})
    assert cfg.analysis_enabled is True
    assert cfg.llm_model == "gemini-claude-opus-4-6-thinking"
    assert cfg.llm_timeout == 120
    assert cfg.analyze_top == 5
    assert cfg.crash_context == ("breadcrumbs", "custom_keys", "user")


def test_analysis_off_without_key_even_with_url():
    cfg = load({**BASE, "LLM_BASE_URL": "http://gw:3000/v1"})
    assert cfg.analysis_enabled is False


def test_analysis_off_when_limit_is_zero():
    """ANALYZE_TOP=0 — штатный способ выключить разбор, не убирая ключ."""
    cfg = load({**BASE, "LLM_BASE_URL": "http://gw:3000/v1",
                "LLM_API_KEY": "ag-key", "ANALYZE_TOP": "0"})
    assert cfg.analysis_enabled is False


def test_base_url_trailing_slash_dropped():
    cfg = load({**BASE, "LLM_BASE_URL": "http://gw:3000/v1/",
                "LLM_API_KEY": "k"})
    assert cfg.llm_base_url == "http://gw:3000/v1"


def test_base_url_without_scheme_is_rejected():
    with pytest.raises(ConfigError) as exc:
        load({**BASE, "LLM_BASE_URL": "gateway.lan:3000"})
    assert "http://" in str(exc.value)


def test_model_can_be_overridden():
    cfg = load({**BASE, "LLM_MODEL": " gemini-claude-sonnet-4-6 "})
    assert cfg.llm_model == "gemini-claude-sonnet-4-6"


def test_blank_model_falls_back_to_default():
    assert load({**BASE, "LLM_MODEL": "  "}).llm_model == \
        "gemini-claude-opus-4-6-thinking"


@pytest.mark.parametrize("key,value", [
    ("LLM_TIMEOUT", "5"), ("LLM_TIMEOUT", "601"),
    ("ANALYZE_TOP", "-1"), ("ANALYZE_TOP", "21"),
])
def test_out_of_range_analysis_numbers_rejected(key, value):
    with pytest.raises(ConfigError):
        load({**BASE, key: value})


def test_crash_context_parsed_and_normalised():
    cfg = load({**BASE, "CRASH_CONTEXT": " Logs , CUSTOM_KEYS "})
    assert cfg.crash_context == ("logs", "custom_keys")


def test_empty_crash_context_means_stacktrace_only():
    assert load({**BASE, "CRASH_CONTEXT": ""}).crash_context == ()


def test_unknown_crash_context_block_rejected():
    """Опечатка иначе молча обеднила бы промпт: разбор стал бы хуже без
    единого признака, что настройка не применилась."""
    with pytest.raises(ConfigError) as exc:
        load({**BASE, "CRASH_CONTEXT": "breadcrumbs,logz"})
    assert "logz" in str(exc.value)
    assert "breadcrumbs" in str(exc.value)
