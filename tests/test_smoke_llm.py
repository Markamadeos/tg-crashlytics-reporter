"""Боевая проверка шлюза моделей. Запускается вручную:

    RUN_SMOKE=1 LLM_BASE_URL=http://host:3000/v1 LLM_API_KEY=... \\
        python3 -m pytest tests/test_smoke_llm.py -v

Отдельный файл, а не тест в test_smoke.py: тот требует доступа к Crashlytics,
а шлюз от него не зависит — доводить промпт надо и без ключа Google.
"""
import os

import pytest

from crashdigest.analysis import (
    SYSTEM_PROMPT,
    build_prompt,
    parse_answer,
    render_comment,
)
from crashdigest.crashlytics import IssueRow, parse_event
from crashdigest.llm import LlmClient
from tests.test_crashlytics import load_fixture

BASE_URL = os.environ.get("LLM_BASE_URL")
API_KEY = os.environ.get("LLM_API_KEY")
MODEL = os.environ.get("LLM_MODEL", "gemini-claude-opus-4-6-thinking")

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_SMOKE") or not (BASE_URL and API_KEY),
    reason="боевой тест, включается RUN_SMOKE=1 + LLM_BASE_URL/LLM_API_KEY",
)

ROW = IssueRow(
    issue_id="iss-1", title="SourceFile - a.b",
    subtitle="java.lang.IllegalStateException - FragmentManager is already "
             "executing transactions",
    error_type="FATAL", state="OPEN", last_seen_version="5.15.0",
    first_seen_version="5.14.0", events_count=29, impacted_users=13,
)


def test_gateway_lists_the_configured_model():
    """Имя модели — параметр compose, и опечатка в нём обязана всплыть здесь,
    а не отказом каждого разбора в проде."""
    import requests

    response = requests.get(
        f"{BASE_URL.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30,
    )
    assert response.status_code == 200, response.text[:200]
    ids = {item["id"] for item in response.json().get("data", [])}
    assert MODEL in ids, f"модели {MODEL} на шлюзе нет; есть: {sorted(ids)}"


def test_gateway_answers_with_parsable_verdict():
    """Живой запрос, разбор ответа, рендер комментария — вся цепочка кроме
    Telegram."""
    client = LlmClient(BASE_URL.rstrip("/"), API_KEY, MODEL, 180)
    event = parse_event(load_fixture("event.json"))

    verdict = parse_answer(
        client.json_object(SYSTEM_PROMPT, build_prompt(event, ROW))
    )

    assert verdict.what_happened or verdict.likely_cause
    assert verdict.checks, "модель не вернула ни одного пункта «что проверить»"

    text = render_comment(verdict, event, ROW, project="p", app_id="a")
    assert "Что произошло" in text
    assert len(text) < 30000


def test_gateway_answer_mentions_the_blamed_developer_frame():
    """Проверка самого промпта, а не шлюза: если модель не опирается на
    виноватый фрейм, разбор бесполезен — он должен указывать место в коде."""
    client = LlmClient(BASE_URL.rstrip("/"), API_KEY, MODEL, 180)
    event = parse_event(load_fixture("event.json"))

    verdict = parse_answer(
        client.json_object(SYSTEM_PROMPT, build_prompt(event, ROW))
    )
    whole = " ".join([verdict.what_happened, verdict.likely_cause, *verdict.checks])
    assert "ProfileFragment" in whole or "onResume" in whole
