import json

import pytest
import requests

from crashdigest.telegram import BACKOFF_SECONDS, MAX_ATTEMPTS, Telegram, TelegramError


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

    def post(self, url, data=None, timeout=None, proxies=None):
        self.calls.append({"url": url, "data": data, "proxies": proxies})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok():
    return FakeResponse(200, {"ok": True, "result": {"message_id": 1}})


def test_sends_each_message_with_rich_message():
    session = FakeSession(ok(), ok())
    Telegram("bt", "-100", session=session, sleep=lambda _: None).send(["a", "b"])

    assert len(session.calls) == 2
    call = session.calls[0]
    assert call["url"] == "https://api.telegram.org/botbt/sendRichMessage"
    assert call["data"]["chat_id"] == "-100"
    assert "rich_message" in call["data"]
    rich = json.loads(call["data"]["rich_message"])
    assert rich["markdown"] == "a"
    assert rich["skip_entity_detection"] is True
    assert call["proxies"] is None


def test_uses_proxy_for_both_schemes_when_configured():
    session = FakeSession(ok())
    Telegram("bt", "-100", proxy="http://p:8888", session=session, sleep=lambda _: None).send(["a"])
    assert session.calls[0]["proxies"] == {
        "http": "http://p:8888",
        "https": "http://p:8888",
    }


def test_retries_on_429_and_honours_retry_after():
    slept = []
    session = FakeSession(
        FakeResponse(429, {"parameters": {"retry_after": 7}}),
        ok(),
    )
    Telegram("bt", "-100", session=session, sleep=slept.append).send(["a"])
    assert slept == [7]
    assert len(session.calls) == 2


def test_retries_on_5xx():
    session = FakeSession(FakeResponse(502, text="bad gateway"), ok())
    Telegram("bt", "-100", session=session, sleep=lambda _: None).send(["a"])
    assert len(session.calls) == 2


def test_gives_up_after_max_attempts():
    session = FakeSession(*[FakeResponse(500, text="boom") for _ in range(MAX_ATTEMPTS)])
    with pytest.raises(TelegramError) as exc:
        Telegram("bt", "-100", session=session, sleep=lambda _: None).send(["a"])
    assert "500" in str(exc.value)


def test_does_not_retry_on_400():
    session = FakeSession(FakeResponse(400, text="chat not found"))
    with pytest.raises(TelegramError) as exc:
        Telegram("bt", "-100", session=session, sleep=lambda _: None).send(["a"])
    assert len(session.calls) == 1
    assert "chat not found" in str(exc.value)


def test_retries_on_transport_error_then_succeeds():
    slept = []
    session = FakeSession(requests.ConnectionError("proxy is dead"), ok())
    Telegram("bt", "-100", session=session, sleep=slept.append).send(["a"])
    assert len(session.calls) == 2
    assert slept == [BACKOFF_SECONDS]


def test_transport_error_exhausted_raises_telegram_error():
    session = FakeSession(
        *[requests.ConnectionError("proxy is dead") for _ in range(MAX_ATTEMPTS)]
    )
    with pytest.raises(TelegramError) as exc:
        Telegram("bt", "-100", session=session, sleep=lambda _: None).send(["a"])
    assert "proxy is dead" in str(exc.value)
    assert len(session.calls) == MAX_ATTEMPTS


def test_transport_error_message_does_not_leak_bot_token():
    token = "123456789:AAH-REAL-TOKEN"
    session = FakeSession(
        *[
            requests.ConnectionError(
                f"HTTPSConnectionPool(host='api.telegram.org', port=443): "
                f"Max retries exceeded with url: /bot{token}/sendMessage "
                f"(Caused by ProxyError(...))"
            )
            for _ in range(MAX_ATTEMPTS)
        ]
    )
    with pytest.raises(TelegramError) as exc:
        Telegram(token, "-100", session=session, sleep=lambda _: None).send(["a"])
    assert token not in str(exc.value)


def test_429_with_non_json_body_falls_back_to_backoff():
    slept = []
    session = FakeSession(
        FakeResponse(429, ValueError("no json"), text="<html>502</html>"), ok()
    )
    Telegram("bt", "-100", session=session, sleep=slept.append).send(["a"])
    assert slept == [BACKOFF_SECONDS]
    assert len(session.calls) == 2


def test_send_uses_rich_message_endpoint_and_markdown():
    """Дневные и недельные сообщения идут новым методом с отключённым автодетектом."""
    session = FakeSession(FakeResponse(200, {"ok": True}))
    Telegram("bt", "-100", session=session, sleep=lambda _: None).send(["# Заголовок"])

    call = session.calls[0]
    assert call["url"].endswith("/sendRichMessage")
    payload = call["data"]
    assert payload["chat_id"] == "-100"
    rich = json.loads(payload["rich_message"])
    assert rich["markdown"] == "# Заголовок"
    assert rich["skip_entity_detection"] is True
    assert "text" not in payload and "parse_mode" not in payload


def test_send_plain_keeps_the_old_endpoint_and_html():
    """Сообщение об отказе обязано дойти, когда сломалось всё остальное."""
    session = FakeSession(FakeResponse(200, {"ok": True}))
    Telegram("bt", "-100", session=session, sleep=lambda _: None).send_plain(["<b>x</b>"])

    call = session.calls[0]
    assert call["url"].endswith("/sendMessage")
    payload = call["data"]
    assert payload["text"] == "<b>x</b>"
    assert payload["parse_mode"] == "HTML"
    assert "rich_message" not in payload


def test_rich_transport_retries_like_the_old_one():
    """Ретраи не должны потеряться при смене метода."""
    session = FakeSession(FakeResponse(500, {}), FakeResponse(200, {"ok": True}))
    Telegram("bt", "-100", session=session, sleep=lambda _: None).send(["x"])
    assert len(session.calls) == 2


def test_rich_transport_scrubs_the_token():
    session = FakeSession(FakeResponse(400, {}, text="url /botSECRET/sendRichMessage failed"))
    with pytest.raises(TelegramError) as exc:
        Telegram("SECRET", "-100", session=session, sleep=lambda _: None).send(["x"])
    assert "SECRET" not in str(exc.value)
