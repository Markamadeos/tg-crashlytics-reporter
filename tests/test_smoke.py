"""Боевая проверка против живого API. Запускается вручную:

    RUN_SMOKE=1 GOOGLE_REFRESH_TOKEN=... GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \\
        CRASHLYTICS_PROJECT=... CRASHLYTICS_APP_ID=... \\
        python3 -m pytest tests/test_smoke.py -v
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from crashdigest.auth import TokenProvider
from crashdigest.crashlytics import CrashlyticsClient
from crashdigest.digest import render
from crashdigest.telegram import Telegram
from crashdigest.versions import recent_versions

PROJECT = os.environ.get("CRASHLYTICS_PROJECT")
APP_ID = os.environ.get("CRASHLYTICS_APP_ID")
REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
TEST_BOT = os.environ.get("TEST_TELEGRAM_BOT_TOKEN")
TEST_CHAT = os.environ.get("TEST_TELEGRAM_CHAT_ID")

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_SMOKE")
    or not (PROJECT and APP_ID and REFRESH_TOKEN and CLIENT_ID and CLIENT_SECRET),
    reason="боевой тест, включается RUN_SMOKE=1 + CRASHLYTICS_PROJECT/CRASHLYTICS_APP_ID/"
           "GOOGLE_REFRESH_TOKEN/GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET",
)


def test_real_api_returns_parsed_rows_for_one_day_window():
    """Окно — сутки, и результат обязан быть непустым.

    На проде это приложение даёт порядка 71 FATAL в сутки, так что пустой ответ
    означает поломку, а не тишину. Часовое окно и проверки внутри `for` не
    годятся: при пустом списке цикл не выполняется ни разу, и тест зеленеет,
    не проверив ни разбора полей, ни фильтра по типу — он прошёл бы даже
    против клиента, который всегда возвращает [].
    """
    tokens = TokenProvider(REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET)
    client = CrashlyticsClient(PROJECT, APP_ID, tokens)

    end = datetime.now(timezone.utc)
    rows = client.top_issues(end - timedelta(days=1), end, ("FATAL",))

    assert rows, "живой API вернул пустой список FATAL за сутки — это поломка"
    assert all(r.issue_id for r in rows)
    assert {r.error_type for r in rows} == {"FATAL"}
    assert all(isinstance(r.events_count, int) and r.events_count > 0 for r in rows)
    assert all(r.last_seen_version for r in rows)


def test_real_api_version_filter_narrows_results():
    """Фильтр по версиям обязан сужать выдачу, иначе он не работает."""
    tokens = TokenProvider(REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET)
    client = CrashlyticsClient(PROJECT, APP_ID, tokens)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=1)

    known = client.list_versions(start, end)
    assert known, "живой API не вернул ни одной версии"

    recent = recent_versions(known, 3)
    assert len(recent.display_versions) == 3

    everything = client.top_issues(start, end, ("FATAL",))
    narrowed = client.top_issues(
        start, end, ("FATAL",), versions=list(recent.display_names)
    )
    assert len(narrowed) <= len(everything)


@pytest.mark.skipif(
    not (TEST_BOT and TEST_CHAT),
    reason="нужны TEST_TELEGRAM_BOT_TOKEN и TEST_TELEGRAM_CHAT_ID",
)
def test_real_delivery_to_test_channel():
    """Сквозная проверка: живые данные превращаются в сообщение и доходят.

    Всё остальное в сюите подменяет Telegram, поэтому доставка — единственное
    звено, которое иначе не проверяется вовсе. Именно оно однажды отказало
    молча: прокси умер и пролежал шесть дней незамеченным.
    """
    tokens = TokenProvider(REFRESH_TOKEN, CLIENT_ID, CLIENT_SECRET)
    client = CrashlyticsClient(PROJECT, APP_ID, tokens)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=1)

    recent = recent_versions(client.list_versions(start, end), 3)
    rows = client.top_issues(
        start, end, ("FATAL",), versions=list(recent.display_names)
    )
    messages = render(
        rows[:3],
        start,
        end,
        app_name="smoke",
        project=PROJECT,
        app_id=APP_ID,
        versions=list(recent.display_versions),
    )

    # Без прокси доставка в этой же сети падает по не относящейся к тесту
    # причине — а именно ради этой сети и нужен TELEGRAM_PROXY в проде.
    Telegram(TEST_BOT, TEST_CHAT, proxy=os.environ.get("TELEGRAM_PROXY") or None).send(messages)
