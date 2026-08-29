"""Сборка Markdown для rich-сообщений Telegram. Без сети и диска."""
from __future__ import annotations

from typing import Sequence

# Ровно тот набор, что проверен на живом API. Отказ при пропуске символа
# молчаливый: Telegram не возвращает ошибку на кривой markdown, он
# возвращает правдоподобно испорченное сообщение.
SPECIAL = r"\`*_{}[]()#+-.!|>~="

_ALIGN = {"l": ":--", "r": "--:"}


def esc(text: str | None) -> str:
    """Экранирует разметку во всём, что пришло из API.

    Перевод строки заменяется пробелом, а не экранируется: внутри ячейки
    таблицы он рвёт строку, и никаким слэшем это не чинится. Многострочный
    текст внутри ячейки собирается тегом <br> на уровне вызывающего.

    `<` экранируется HTML-сущностью, а не слэшем: формат этой ветки
    намеренно заставляет Telegram разбирать наши собственные HTML-теги
    (<br>, <details>) внутри markdown, и любой `<`, пришедший из API
    (например, `<init>` в java-стектрейсах), попадает в тот же парсер как
    потенциальное начало тега. Проверено на живом API: экранированный
    слэшем `<b>` возвращается с тегом всё ещё разобранным (слэш не
    защищает), а HTML-сущность `&lt;b&gt;` — как ровно `<b>`. `&`
    экранируется первым, иначе буквальный `&lt;` во входе раскрылся бы
    получателем в настоящую угловую скобку.
    """
    if not text:
        return ""
    text = text.replace("\n", " ").replace("&", "&amp;").replace("<", "&lt;")
    return "".join("\\" + ch if ch in SPECIAL else ch for ch in text)


def link(text: str, url: str) -> str:
    """Текст обязан быть уже экранированным. URL экранируется только на ) и \\ (Telegram разбирает разметку внутри ())."""
    escaped_url = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{text}]({escaped_url})"


def table(header: Sequence[str], rows: Sequence[Sequence[str]], align: str) -> str:
    """Markdown-таблица. `align` — по букве на колонку: l или r."""
    if len(align) != len(header):
        raise ValueError(
            f"выравнивание задано на {len(align)} колонок, а колонок {len(header)}"
        )
    for idx, row in enumerate(rows):
        if len(row) != len(header):
            raise ValueError(
                f"строка {idx}: задано {len(row)} ячеек, а в заголовке {len(header)}"
            )
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(_ALIGN[a] for a in align) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)
