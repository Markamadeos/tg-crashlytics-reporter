"""Access-токен для Crashlytics по ключу сервисного аккаунта."""
from __future__ import annotations

import google.auth.transport.requests
from google.auth import exceptions as google_exceptions
from google.oauth2 import service_account

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
KEY_HINT = "JSON-ключ из Google Cloud Console: IAM → Сервисные аккаунты → Ключи"


class AuthError(Exception):
    """Не удалось получить access-токен."""


class TokenProvider:
    """Отдаёт свежий access-токен, обновляя его по мере устаревания.

    Ключ читается сразу, в конструкторе: неверный путь или не тот файл должен
    всплыть при старте контейнера, а не в плановый час прогона. Тексты ошибок
    уходят прямо в Telegram-канал, поэтому обязаны быть точными — и никогда
    не содержат содержимого файла.
    """

    def __init__(self, credentials_path: str, request=None):
        self._path = credentials_path
        self._request = request or google.auth.transport.requests.Request()
        try:
            self._credentials = service_account.Credentials.from_service_account_file(
                credentials_path, scopes=SCOPES
            )
        except OSError as exc:
            raise AuthError(
                "не удалось прочитать ключ сервисного аккаунта "
                f"{credentials_path}: {exc.strerror or exc}"
            ) from exc
        except (ValueError, google_exceptions.GoogleAuthError) as exc:
            raise AuthError(
                f"{credentials_path} — не ключ сервисного аккаунта ({exc}). "
                f"Нужен {KEY_HINT}"
            ) from exc

    def token(self) -> str:
        if not self._credentials.valid:
            try:
                self._credentials.refresh(self._request)
            except google_exceptions.RefreshError as exc:
                raise AuthError(self._refresh_error_message(exc)) from exc
            except google_exceptions.TransportError as exc:
                raise AuthError(
                    f"не удалось связаться с Google для получения токена: {exc}"
                ) from exc
        return self._credentials.token

    def _refresh_error_message(self, exc: google_exceptions.RefreshError) -> str:
        """Совет выпустить новый ключ уместен только при invalid_grant: так
        Google отвечает на отозванный ключ, удалённую или отключённую учётку
        и сбитые часы. Прочие отказы просто пересказываем — ответ бывает и
        не JSON вовсе.
        """
        email = self._credentials.service_account_email
        detail = str(exc.args[0] if exc.args else exc)[:200]
        payload = exc.args[1] if len(exc.args) > 1 else None
        error = payload.get("error") if isinstance(payload, dict) else None

        if error == "invalid_grant":
            return (
                f"Google отклонил ключ сервисного аккаунта {email} ({detail}). "
                "Ключ отозван, либо учётка удалена или отключена — выпустите новый ключ "
                f"({KEY_HINT}) и положите его в {self._path}. "
                "Если ключ точно жив, проверьте часы на хосте"
            )
        return f"Google отказал в выдаче токена для {email}: {detail}"
