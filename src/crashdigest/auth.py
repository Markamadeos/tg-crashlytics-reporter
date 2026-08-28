"""Обмен refresh-токена на access-токен без участия браузера."""
from __future__ import annotations

import time

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
REFRESH_MARGIN_SECONDS = 60


class AuthError(Exception):
    """Не удалось получить access-токен."""


def _error_message(response) -> str:
    """Текст уходит прямо в Telegram-канал, поэтому обязан быть точным.

    Отозванный токен (invalid_grant) — единственный случай, когда совет
    заново залогиниться верен. На 500 или сетевой сбой прокси такой совет
    вводит в заблуждение: тело может вовсе не быть JSON, поэтому парсим
    защитно и просто пересказываем, что ответил сам Google.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        payload = {}

    error = payload.get("error")
    description = payload.get("error_description")

    if error == "invalid_grant":
        return (
            "refresh-токен отклонён Google "
            f"(HTTP {response.status_code}). Нужен повторный вход: "
            "npx firebase-tools login (или firebase login, если Firebase CLI установлен глобально), "
            "затем перенести refresh_token "
            "из ~/.config/configstore/firebase-tools.json в GOOGLE_REFRESH_TOKEN"
        )

    detail = ": ".join(part for part in (error, description) if part)
    if not detail:
        detail = (response.text or "")[:200]
    return f"Google вернул HTTP {response.status_code} при обновлении токена: {detail}"


class TokenProvider:
    """Отдаёт свежий access-токен, обновляя его по мере устаревания."""

    def __init__(self, refresh_token: str, client_id: str, client_secret: str,
                 session=None, clock=time.monotonic):
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        if self._token is not None and self._clock() < self._expires_at:
            return self._token

        response = self._session.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise AuthError(_error_message(response))

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthError("ответ Google не содержит access_token")

        expires_in = float(payload.get("expires_in", 3600))
        self._token = token
        self._expires_at = self._clock() + expires_in - REFRESH_MARGIN_SECONDS
        return token
