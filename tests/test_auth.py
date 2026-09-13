import json

import pytest
from google.auth import exceptions as google_exceptions

from crashdigest.auth import AuthError, TokenProvider


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.headers = {}
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self.data = body.encode()


class FakeRequest:
    """Транспорт в том виде, в каком его вызывает google-auth."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "method": method, "body": body})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def ok(token, expires_in=3600):
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in})


def test_exchanges_service_account_key_for_access_token(service_account_key):
    request = FakeRequest(ok("at-1"))
    provider = TokenProvider(service_account_key, request=request)

    assert provider.token() == "at-1"
    call = request.calls[0]
    assert call["url"] == "https://oauth2.googleapis.com/token"
    assert b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in call["body"]


def test_token_is_reused_while_valid(service_account_key):
    request = FakeRequest(ok("at-1"), ok("at-2"))
    provider = TokenProvider(service_account_key, request=request)

    assert provider.token() == "at-1"
    assert provider.token() == "at-1"
    assert len(request.calls) == 1


def test_token_close_to_expiry_is_refreshed(service_account_key):
    request = FakeRequest(ok("at-1", expires_in=10), ok("at-2"))
    provider = TokenProvider(service_account_key, request=request)

    assert provider.token() == "at-1"
    assert provider.token() == "at-2"


def test_missing_key_file_fails_at_construction_naming_the_path(tmp_path):
    path = str(tmp_path / "nope.json")
    with pytest.raises(AuthError) as exc:
        TokenProvider(path, request=FakeRequest())
    assert path in str(exc.value)


def test_non_service_account_json_is_rejected_with_explanation(tmp_path):
    """Самая вероятная ошибка оператора — подложить файл от firebase-tools или
    gcloud вместо ключа сервисного аккаунта.
    """
    path = tmp_path / "user.json"
    path.write_text(json.dumps({
        "type": "authorized_user", "refresh_token": "rt",
        "client_id": "c", "client_secret": "s",
    }))
    with pytest.raises(AuthError) as exc:
        TokenProvider(str(path), request=FakeRequest())
    message = str(exc.value)
    assert str(path) in message
    assert "сервисного аккаунта" in message


def test_rejected_key_raises_auth_error_with_actionable_text(service_account_key):
    request = FakeRequest(FakeResponse(400, {
        "error": "invalid_grant", "error_description": "Invalid JWT Signature.",
    }))
    provider = TokenProvider(service_account_key, request=request)

    with pytest.raises(AuthError) as exc:
        provider.token()
    message = str(exc.value)
    with open(service_account_key) as key:
        assert json.load(key)["client_email"] in message
    assert "Invalid JWT Signature." in message
    assert "новый ключ" in message


def test_other_token_errors_are_reported_without_key_advice(service_account_key):
    request = FakeRequest(FakeResponse(401, {
        "error": "unauthorized_client", "error_description": "client is disabled",
    }))
    provider = TokenProvider(service_account_key, request=request)

    with pytest.raises(AuthError) as exc:
        provider.token()
    message = str(exc.value)
    assert "client is disabled" in message
    assert "новый ключ" not in message


def test_network_failure_raises_auth_error(service_account_key):
    request = FakeRequest(google_exceptions.TransportError("connection reset"))
    provider = TokenProvider(service_account_key, request=request)

    with pytest.raises(AuthError) as exc:
        provider.token()
    assert "connection reset" in str(exc.value)
