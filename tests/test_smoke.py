"""Боевая проверка против живого API. Запускается вручную:

    RUN_SMOKE=1 GOOGLE_REFRESH_TOKEN=... GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \\
        CRASHLYTICS_PROJECT=... CRASHLYTICS_APP_ID=... \\
        python3 -m pytest tests/test_smoke.py -v
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
import requests

from crashdigest.auth import TokenProvider
from crashdigest.crashlytics import CrashlyticsClient
from crashdigest.digest import render, _split_subtitle
from crashdigest.telegram import Telegram
from crashdigest.versions import recent_versions
from tests.test_crashlytics import load_fixture

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


def _send_rich_message(text: str) -> dict:
    """POST sendRichMessage тестовому боту. Обвязка общая для обоих живых
    rich-тестов ниже: раньше её дублировали, и прокси прокидывал только
    один из двух вызовов — на NAS, где Telegram доступен только через
    TELEGRAM_PROXY, второй тест молча падал бы по не относящейся к делу
    сетевой причине.
    """
    proxy = os.environ.get("TELEGRAM_PROXY")
    response = requests.post(
        f"https://api.telegram.org/bot{TEST_BOT}/sendRichMessage",
        data={"chat_id": TEST_CHAT,
              "rich_message": json.dumps({"markdown": text, "skip_entity_detection": True})},
        timeout=30,
        proxies={"http": proxy, "https": proxy} if proxy else None,
    )
    body = response.json()
    assert body["ok"], body
    return body


@pytest.mark.skipif(not (TEST_BOT and TEST_CHAT),
                    reason="нужны TEST_TELEGRAM_BOT_TOKEN и TEST_TELEGRAM_CHAT_ID")
def test_real_rich_delivery_keeps_the_link_inside_a_table_cell():
    """Ссылка в ячейке живёт только в markdown-режиме: в blocks Telegram
    молча её вырезает. Регрессия здесь тихая, поэтому проверяем на живом API.
    """
    from crashdigest import markdown as md

    url = "https://example.com/i/1"
    text = "\n".join([
        "# Проба доставки",
        "",
        md.table(("Ошибка", "События"),
                 [[md.link(md.esc("IllegalStateException"), url), "42"]],
                 "lr"),
    ])
    body = _send_rich_message(text)
    cell = body["result"]["rich_message"]["blocks"][1]["cells"][1][0]
    # Ячейка — обёртка {"text": ..., "align": ..., "valign": ...}: сама
    # ссылка на уровень глубже, в cell["text"] (проверено на живом ответе).
    # Эта ячейка содержит ровно одну ссылку, поэтому cell["text"] — dict
    # вида {"type": "url", "text": ..., "url": ...}, а не list (list бывает
    # у ячейки с несколькими частями). Прямая индексация без .get() —
    # нарочно: если форма ответа окажется другой, тест обязан упасть
    # громко (KeyError/TypeError), а не молча сравнить None с None.
    link = cell["text"]
    assert link["url"] == url, f"ссылка вырезана или испорчена: {cell!r}"


@pytest.mark.skipif(not (TEST_BOT and TEST_CHAT),
                    reason="нужны TEST_TELEGRAM_BOT_TOKEN и TEST_TELEGRAM_CHAT_ID")
def test_real_escaping_survives_round_trip_on_live_crash_subtitles():
    """Пропущенный символ не даёт ошибки — он молча портит разметку.
    Поэтому сверяем не глазами, а с тем, что Telegram вернул в разобранном виде.
    """
    from crashdigest import markdown as md
    from crashdigest.crashlytics import parse_report

    rows = parse_report(load_fixture("top_issues.json"))
    subtitles = [_split_subtitle(r.subtitle)[1] for r in rows]
    # < и & сюда не попадают ни из фикстуры, ни из литерала спецсимволов
    # SPECIAL — а именно '<' был единственным символом разметки, который
    # esc() не экранировал (Important 1 финального ревью).
    subtitles.append(r"all specials: \ ` * _ { } [ ] ( ) # + - . ! | > ~ = < & end")
    subtitles = [s for s in subtitles if s]

    text = md.table(("Текст",), [[md.esc(s)] for s in subtitles], "l")
    body = _send_rich_message(text)

    cells = body["result"]["rich_message"]["blocks"][0]["cells"][1:]
    assert len(cells) == len(subtitles), f"Telegram вернул {len(cells)} строк, отправили {len(subtitles)}"
    for original, cell in zip(subtitles, cells):
        got = cell[0]["text"]
        assert got == original, f"разметка съела символы: {original!r} -> {got!r}"
