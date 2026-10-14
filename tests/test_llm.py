import pytest
import requests

from crashdigest.llm import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    LlmClient,
    LlmError,
    extract_json,
)

KEY = "ag-secret-key"


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def answer(content):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def client(*responses, timeout=120):
    session = FakeSession(*responses)
    return LlmClient("http://gw:3000/v1/", KEY, "model-x", timeout,
                     session=session, sleep=lambda _: None), session


# --- extract_json ----------------------------------------------------------

def test_extract_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_strips_json_fence():
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_extract_finds_object_inside_prose():
    """Thinking-модель иногда предваряет объект фразой. Это не повод терять
    разбор целиком."""
    assert extract_json('Вот результат:\n{"a": 3}\nготово') == {"a": 3}


def test_extract_rejects_non_object():
    with pytest.raises(LlmError) as exc:
        extract_json("[1, 2]")
    assert "не объект" in str(exc.value)


def test_extract_rejects_garbage():
    with pytest.raises(LlmError):
        extract_json("совсем не json")


# --- запрос ----------------------------------------------------------------

def test_sends_chat_completions_with_json_mode():
    llm, session = client(answer('{"ok": 1}'))
    assert llm.json_object("систем", "юзер") == {"ok": 1}

    call = session.calls[0]
    assert call["url"] == "http://gw:3000/v1/chat/completions"
    assert call["json"]["model"] == "model-x"
    assert call["json"]["response_format"] == {"type": "json_object"}
    assert call["json"]["messages"] == [
        {"role": "system", "content": "систем"},
        {"role": "user", "content": "юзер"},
    ]
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["timeout"] == 120


def test_ignores_environment_proxies():
    """Шлюз — сосед по локальной сети. Прокси для Telegram из HTTP_PROXY увёл
    бы запрос во внешний мир, откуда он не вернётся."""
    llm, session = client(answer("{}"))
    assert session.trust_env is False


def test_retries_on_server_error_then_succeeds():
    llm, session = client(FakeResponse(503), answer('{"ok": 2}'))
    assert llm.json_object("s", "u") == {"ok": 2}
    assert len(session.calls) == 2


def test_retries_on_rate_limit():
    llm, session = client(FakeResponse(429), answer('{"ok": 3}'))
    assert llm.json_object("s", "u") == {"ok": 3}
    assert len(session.calls) == 2


def test_client_error_is_not_retried():
    llm, session = client(FakeResponse(400, text="bad request"))
    with pytest.raises(LlmError) as exc:
        llm.json_object("s", "u")
    assert "400" in str(exc.value)
    assert len(session.calls) == 1


def test_gives_up_after_max_attempts():
    llm, session = client(*[FakeResponse(500)] * MAX_ATTEMPTS)
    with pytest.raises(LlmError):
        llm.json_object("s", "u")
    assert len(session.calls) == MAX_ATTEMPTS


def test_transport_failure_becomes_llm_error():
    llm, session = client(*[requests.ConnectionError("нет маршрута")] * MAX_ATTEMPTS)
    with pytest.raises(LlmError) as exc:
        llm.json_object("s", "u")
    assert "недоступен" in str(exc.value)


def test_backoff_grows_between_attempts():
    delays = []
    session = FakeSession(FakeResponse(500), FakeResponse(500), answer("{}"))
    llm = LlmClient("http://gw/v1", KEY, "m", 10, session=session,
                    sleep=delays.append)
    llm.json_object("s", "u")
    assert delays == [BACKOFF_SECONDS, BACKOFF_SECONDS * 2]


# --- ключ не утекает -------------------------------------------------------

def test_key_scrubbed_from_http_error():
    llm, _ = client(FakeResponse(401, text=f"invalid key {KEY}"))
    with pytest.raises(LlmError) as exc:
        llm.json_object("s", "u")
    assert KEY not in str(exc.value)
    assert "***" in str(exc.value)


def test_key_scrubbed_from_transport_error():
    boom = requests.ConnectionError(f"Bearer {KEY} refused")
    llm, _ = client(*[boom] * MAX_ATTEMPTS)
    with pytest.raises(LlmError) as exc:
        llm.json_object("s", "u")
    assert KEY not in str(exc.value)


# --- кривые ответы шлюза ---------------------------------------------------

def test_non_json_body_is_reported():
    llm, _ = client(FakeResponse(200, ValueError("не json"), text="<html>502</html>"))
    with pytest.raises(LlmError) as exc:
        llm.json_object("s", "u")
    assert "не JSON" in str(exc.value)


def test_missing_choices_is_reported():
    llm, _ = client(FakeResponse(200, {"error": "model not found"}))
    with pytest.raises(LlmError) as exc:
        llm.json_object("s", "u")
    assert "chat/completions" in str(exc.value)


def test_empty_content_is_reported():
    llm, _ = client(answer("   "))
    with pytest.raises(LlmError) as exc:
        llm.json_object("s", "u")
    assert "пустой" in str(exc.value)
