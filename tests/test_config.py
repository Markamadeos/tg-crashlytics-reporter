import pytest
from crashdigest.config import Config, ConfigError, load

BASE = {
    "CRASHLYTICS_PROJECT": "example-prod",
    "CRASHLYTICS_APP_ID": "1:111111111111:android:0000000000000000",
    "GOOGLE_REFRESH_TOKEN": "rt",
    "GOOGLE_CLIENT_ID": "cid",
    "GOOGLE_CLIENT_SECRET": "csecret",
    "TELEGRAM_BOT_TOKEN": "bt",
    "TELEGRAM_CHAT_ID": "-1000000000000",
}


def test_loads_required_fields_and_applies_defaults():
    cfg = load(BASE)
    assert isinstance(cfg, Config)
    assert cfg.project == "example-prod"
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
    assert "GOOGLE_REFRESH_TOKEN" in message
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
