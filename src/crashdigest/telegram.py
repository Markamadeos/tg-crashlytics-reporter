"""Доставка сообщений в Telegram, при необходимости через HTTP-прокси."""
from __future__ import annotations

import json
import time
from typing import Sequence

import requests

API = "https://api.telegram.org"
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 5

# Сколько ждём автофорварда поста в группу обсуждений. Обычно он приходит за
# секунду-две, но это Telegram, и дешевле подождать, чем потерять разбор.
COMMENT_WAIT_SECONDS = 60
# Длинный polling: один запрос вместо десяти коротких за тот же интервал.
UPDATES_TIMEOUT = 10


class TelegramError(Exception):
    """Сообщение не доставлено."""


_UNSET = object()


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
        # _UNSET, а не None: «ещё не спрашивали» и «канал без обсуждений» —
        # разные состояния, и второе не должно приводить к новому запросу
        # на каждое падение в дайджесте.
        self._linked: str | None | object = _UNSET

    def _scrub(self, text: str) -> str:
        """Токен вшит в URL, и requests/urllib3 кладют его в текст сетевых
        исключений целиком. Эта строка обычно летит и в stdout контейнера
        (persisted docker-логи), и — при отказе доставки — в сам канал, так
        что токен обязан быть вычищен из неё до того, как она куда-то уйдёт.
        """
        return text.replace(self._token, "***")

    def _rich(self, chat_id: str, message: str,
              reply_to: int | None = None) -> dict:
        payload = {
            "chat_id": chat_id,
            # skip_entity_detection: иначе `#` в тексте краша станет
            # хэштегом даже экранированный — проверено на живом API.
            "rich_message": json.dumps(
                {"markdown": message, "skip_entity_detection": True}
            ),
        }
        if reply_to is not None:
            payload["reply_to_message_id"] = str(reply_to)
        return payload

    def send(self, messages: Sequence[str]) -> list[int | None]:
        """Дневные, недельные, стартовые сообщения — Markdown через rich.

        Возвращает id отправленных постов: под дневным постом потом вешаются
        комментарии с разбором, а найти его копию в группе обсуждений без id
        невозможно.
        """
        sent: list[int | None] = []
        for message in messages:
            result = self._result(
                f"{self._base}/sendRichMessage", self._rich(self._chat_id, message)
            )
            sent.append(
                result.get("message_id") if isinstance(result, dict) else None
            )
        return sent

    def linked_chat_id(self) -> str | None:
        """Группа обсуждений канала — та, где живут комментарии.

        Берётся у самого Telegram, а не из настроек: иначе в конфиге появился
        бы второй chat_id, который обязан совпадать с привязкой канала, и
        расхождение между ними никак не обнаруживалось бы до первого
        комментария, ушедшего не туда.
        """
        if self._linked is _UNSET:
            result = self._result(f"{self._base}/getChat", {"chat_id": self._chat_id})
            linked = result.get("linked_chat_id") if isinstance(result, dict) else None
            self._linked = str(linked) if linked is not None else None
        return self._linked

    def updates_cursor(self) -> int | None:
        """Номер, с которого смотреть обновления. Снимается ДО отправки поста.

        Дренаж чужих обновлений здесь безопасен только потому, что этот бот не
        читает их ни для чего: ни команд, ни ответов он не обрабатывает. Если
        когда-нибудь начнёт — этот метод придётся переписать на webhook,
        потому что getUpdates и webhook взаимоисключающи.
        """
        result = self._result(
            f"{self._base}/getUpdates",
            {"offset": "-1", "timeout": "0",
             "allowed_updates": json.dumps(["message"])},
        )
        if not isinstance(result, list) or not result:
            return None
        return max(update.get("update_id", -1) for update in result) + 1

    def find_discussion_copy(self, post_id: int, *, cursor: int | None,
                             wait_seconds: int = COMMENT_WAIT_SECONDS,
                             clock=time.monotonic) -> int | None:
        """id автофорварда поста в группе обсуждений — к нему и шлём ответ.

        Telegram не сообщает этот id в ответе на отправку поста: копия в группе
        появляется отдельным обновлением. Поэтому единственный путь — поймать
        её в getUpdates. Бот обязан быть админом группы обсуждений, иначе
        обновление до него просто не дойдёт.
        """
        deadline = clock() + wait_seconds
        offset = cursor
        while True:
            payload = {"timeout": str(UPDATES_TIMEOUT),
                       "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                payload["offset"] = str(offset)

            result = self._result(f"{self._base}/getUpdates", payload)
            for update in result if isinstance(result, list) else []:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = max(offset or 0, update_id + 1)

                message = update.get("message") or {}
                if not message.get("is_automatic_forward"):
                    continue
                # forward_origin — нынешнее поле, forward_from_message_id —
                # устаревшее. Какое придёт, зависит от версии Bot API на той
                # стороне, поэтому смотрим оба.
                origin = message.get("forward_origin") or {}
                origin_id = origin.get("message_id")
                if origin_id is None:
                    origin_id = message.get("forward_from_message_id")
                if origin_id == post_id:
                    return message.get("message_id")

            if clock() >= deadline:
                return None

    def send_comment(self, message: str, *, chat_id: str, reply_to: int) -> None:
        """Комментарий под постом: ответ на автофорвард в группе обсуждений."""
        self._post(
            f"{self._base}/sendRichMessage",
            self._rich(chat_id, message, reply_to=reply_to),
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
        """Запрос, чей ответ не нужен."""
        self._result(url, payload)

    def _result(self, url: str, payload: dict):
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
                return self._body(response)

            if response.status_code == 409:
                # Единственный источник 409 здесь — висящий webhook: с ним
                # getUpdates не работает в принципе, и повторять запрос
                # бессмысленно. Причину надо назвать, а не прятать за «HTTP 409».
                raise TelegramError(
                    "Telegram вернул HTTP 409: у бота активен webhook, и пока он "
                    "висит, getUpdates недоступен — а без него не найти пост в "
                    "группе обсуждений. Снимите webhook (deleteWebhook)."
                )

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or last:
                raise TelegramError(
                    f"Telegram вернул HTTP {response.status_code}: "
                    f"{self._scrub(response.text[:200])}"
                )

            self._sleep(_retry_delay(response, attempt))

    def _body(self, response):
        """Поле `result` ответа Telegram."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram ответил не JSON: {self._scrub(response.text[:200])}"
            ) from exc
        return payload.get("result") if isinstance(payload, dict) else None
