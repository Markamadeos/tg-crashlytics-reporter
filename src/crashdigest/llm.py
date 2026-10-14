"""Клиент OpenAI-совместимого шлюза моделей. Только chat/completions."""
from __future__ import annotations

import json
import re
import time

import requests

MAX_TOKENS = 1200
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 5

# Ограждение ```json вокруг ответа. Шлюзу передаётся response_format, но
# thinking-модели всё равно иногда оборачивают объект — проверено на живом API.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class LlmError(Exception):
    """Шлюз не ответил, ответил не тем или ответил не JSON-объектом."""


def extract_json(text: str) -> dict:
    """JSON-объект из ответа модели, даже если он обёрнут текстом.

    Падать на лишней строке вокруг объекта незачем: разбор краша — вещь
    необязательная, а вот потерять его из-за ```json было бы обидно.
    """
    stripped = _FENCE.sub("", text.strip())
    try:
        value = json.loads(stripped)
    except ValueError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise LlmError(
                f"в ответе модели нет JSON-объекта: {stripped[:200]!r}"
            ) from None
        try:
            value = json.loads(stripped[start:end + 1])
        except ValueError as exc:
            raise LlmError(f"ответ модели не разбирается как JSON: {exc}") from exc

    if not isinstance(value, dict):
        raise LlmError(f"ответ модели не объект, а {type(value).__name__}")
    return value


class LlmClient:
    """Один метод: задать вопрос и получить JSON-объект.

    Форма retry-цикла взята с `telegram.Telegram._post` намеренно: там он уже
    отлажен на живых отказах, и два разных цикла в одном проекте пришлось бы
    чинить дважды.
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int,
                 session=None, sleep=time.sleep):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._session = session or requests.Session()
        # Шлюз — адрес в локальной сети, а в окружении контейнера живёт
        # TELEGRAM_PROXY. Если он же окажется в HTTP_PROXY/HTTPS_PROXY,
        # requests молча уведёт запрос к соседу по LAN через внешний прокси —
        # и он не вернётся. Переменные окружения здесь не нужны вообще.
        self._session.trust_env = False
        self._sleep = sleep

    @property
    def model(self) -> str:
        return self._model

    def _scrub(self, text: str) -> str:
        """Ключ шлюза не должен попасть ни в docker-логи, ни в Telegram.

        Та же причина, что у `Telegram._scrub`: текст сетевого исключения
        уходит в stdout контейнера, а при отказе — ещё и в канал.
        """
        return text.replace(self._key, "***") if self._key else text

    def json_object(self, system: str, user: str) -> dict:
        payload = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base}/chat/completions"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                response = self._session.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as exc:
                if last:
                    raise LlmError(
                        f"шлюз моделей недоступен после {MAX_ATTEMPTS} попыток: "
                        f"{self._scrub(str(exc))}"
                    ) from exc
                self._sleep(BACKOFF_SECONDS * attempt)
                continue

            if response.status_code == 200:
                return extract_json(self._content(response))

            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or last:
                raise LlmError(
                    f"шлюз моделей вернул HTTP {response.status_code}: "
                    f"{self._scrub(response.text[:200])}"
                )
            self._sleep(BACKOFF_SECONDS * attempt)

        raise LlmError("шлюз моделей не ответил")  # недостижимо, для линтера

    def _content(self, response) -> str:
        """Текст первого choice. Любая другая форма — отказ с внятной причиной."""
        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmError(
                f"шлюз ответил не JSON: {self._scrub(response.text[:200])}"
            ) from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(
                f"ответ шлюза не похож на chat/completions: "
                f"{self._scrub(json.dumps(payload)[:200])}"
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise LlmError("шлюз вернул пустой ответ модели")
        return content
