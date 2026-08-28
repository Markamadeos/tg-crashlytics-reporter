import pytest
from crashdigest.auth import AuthError, TokenProvider


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data))
        return self.responses.pop(0)


def test_returns_access_token():
    session = FakeSession(FakeResponse(200, {"access_token": "at-1", "expires_in": 3600}))
    provider = TokenProvider("rt", "cid", "csecret", session=session, clock=lambda: 0.0)
    assert provider.token() == "at-1"
    url, data = session.calls[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "rt"


def test_token_is_cached_until_near_expiry():
    session = FakeSession(
        FakeResponse(200, {"access_token": "at-1", "expires_in": 3600}),
        FakeResponse(200, {"access_token": "at-2", "expires_in": 3600}),
    )
    now = [0.0]
    provider = TokenProvider("rt", "cid", "csecret", session=session, clock=lambda: now[0])

    assert provider.token() == "at-1"
    now[0] = 3000.0
    assert provider.token() == "at-1"      # ещё свежий
    now[0] = 3550.0
    assert provider.token() == "at-2"      # обновился за 60с до истечения
    assert len(session.calls) == 2


def test_revoked_token_raises_auth_error_with_actionable_text():
    session = FakeSession(
        FakeResponse(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    )
    provider = TokenProvider("rt", "cid", "csecret", session=session, clock=lambda: 0.0)
    with pytest.raises(AuthError) as exc:
        provider.token()
    assert "firebase login" in str(exc.value)


def test_server_error_reports_status_and_reason_without_relogin_advice():
    session = FakeSession(
        FakeResponse(500, {"error": "internal_error", "error_description": "backend is unavailable"})
    )
    provider = TokenProvider("rt", "cid", "csecret", session=session, clock=lambda: 0.0)
    with pytest.raises(AuthError) as exc:
        provider.token()
    message = str(exc.value)
    assert "500" in message
    assert "backend is unavailable" in message
    assert "firebase login" not in message
