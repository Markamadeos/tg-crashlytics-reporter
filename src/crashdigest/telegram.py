"""Доставка сообщений в Telegram, при необходимости через HTTP-прокси."""
from __future__ import annotations

import json
import time
from typing import Sequence

import requests

API = "https://api.telegram.org"
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 5


class TelegramError(Exception):
    """Сообщение не доставлено."""


def _retry_delay(response, attempt: int) -> float:
    """429 несёт своё значение задержки; всё прочее — линейный откат.

    Тело 429 не обязано быть JSON: перед прокси может ответить сам прокси
    или шлюз, и тогда .json() бросит ValueError вместо TelegramError.
    """
    if response.status_code != 429:
        return BACKOFF_SECONDS * attempt
    try:
        return response.json().get("parameters", {}).get("retry_after", BACKOFF_SECONDS)
    except ValueError:
        return BACKOFF_SECONDS


class Telegram:
    def __init__(self, bot_token: str, chat_id: str, proxy: str | None = None,
                 session=None, sleep=time.sleep):
        self._token = bot_token
        self._base = f"{API}/bot{bot_token}"
        self._chat_id = chat_id
        self._proxies = {"http": proxy, "https": proxy} if proxy else None
        self._session = session or requests.Session()
        self._sleep = sleep

    def _scrub(self, text: str) -> str:
        """Токен вшит в URL, и requests/urllib3 кладют его в текст сетевых
        исключений целиком. Эта строка обычно летит и в stdout контейнера
        (persisted docker-логи), и — при отказе доставки — в сам канал, так
        что токен обязан быть вычищен из неё до того, как она куда-то уйдёт.
        """
        return text.replace(self._token, "***")

    def send(self, messages: Sequence[str]) -> None:
        """Дневные, недельные, стартовые сообщения — Markdown через rich."""
        for message in messages:
            self._post(
                f"{self._base}/sendRichMessage",
                {
                    "chat_id": self._chat_id,
                    # skip_entity_detection: иначе `#` в тексте краша станет
                    # хэштегом даже экранированный — проверено на живом API.
                    "rich_message": json.dumps(
                        {"markdown": message, "skip_entity_detection": True}
                    ),
                },
            )

    def send_plain(self, messages: Sequence[str]) -> None:
        """Сообщения об отказе. Намеренно старый метод: он обязан дойти
        именно тогда, когда сломалось всё остальное, включая сам rich.
        """
        for message in messages:
            self._post(
                f"{self._base}/sendMessage",
                {
                    "chat_id": self._chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )

    def _post(self, url: str, payload: dict) -> None:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                response = self._session.post(
                    url, data=payload, timeout=60, proxies=self._proxies
                )
            except requests.RequestException as exc:
                # Прокси, через который идёт этот запрос, уже умирал незамеченным
                # на шесть дней. Обрыв транспорта здесь — штатный сценарий, и он
                # обязан стать громким TelegramError, а не голым исключением requests.
                if last:
                    raise TelegramError(
                        f"Telegram недоступен после {MAX_ATTEMPTS} попыток: "
                        f"{self._scrub(str(exc))}"
                    ) from exc
                self._sleep(BACKOFF_SECONDS * attempt)
                continue

            if response.status_code == 200:
                return

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or last:
                raise TelegramError(
                    f"Telegram вернул HTTP {response.status_code}: "
                    f"{self._scrub(response.text[:200])}"
                )

            self._sleep(_retry_delay(response, attempt))
