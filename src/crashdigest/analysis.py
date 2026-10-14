"""Разбор краша: промпт для модели и рендер её ответа. Без сети и диска."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from crashdigest import markdown as md
from crashdigest.crashlytics import CrashEvent, EventFrame, IssueRow
from crashdigest.digest import issue_url, plural

# Фреймов на один стек в промпте. Полный android-стек бывает в сотню фреймов,
# а полезны верхние: ниже идут Looper, ActivityThread и ZygoteInit.
MAX_FRAMES = 40
# Строк в таблице «Где падает». Таблица — не стек, а указание места.
TABLE_ROWS = 6
MAX_CHECKS = 4

# Заголовки секций. Эмодзи — не украшение: в ветке комментариев это единственный
# якорь, по которому глаз находит нужный раздел, не читая его целиком.
SECTIONS = {
    "what": "💥 Что произошло",
    "where": "📍 Где падает",
    "cause": "🔍 Вероятная причина",
    "checks": "✅ Что проверить",
    "context": "📋 Контекст",
}

# Бюджет на имя символа в ячейке таблицы.
SYMBOL_BUDGET = 30

# Java пишет `SourceFile`, когда настоящего имени файла в классе нет. У
# обфусцированной сборки так помечены ВСЕ фреймы подряд, и как подсказка это
# имя не стоит ни одного символа ячейки.
PLACEHOLDER_FILES = ("SourceFile", "Unknown Source", "")

# Длины полей ответа. Запас к запрошенному у модели взят не случайно: на живых
# крашах она переписывает заданный лимит на треть-половину, и обрыв приходится
# на середину фразы. Проси меньше, чем готов принять — тогда усечение остаётся
# предохранителем от простыни, а не штатным поведением.
LIMIT_WHAT = 450
LIMIT_CAUSE = 900
LIMIT_CHECK = 280

SYSTEM_PROMPT = """\
Ты — опытный мобильный разработчик. По стектрейсу и обстановке падения даёшь \
короткий разбор для команды, которая этот код поддерживает.

Отвечай ТОЛЬКО JSON-объектом такой формы:
{"what_happened": "...", "likely_cause": "...", "checks": ["...", "..."]}

- what_happened — что именно произошло, 1–2 предложения, до 300 символов. \
Если исключений несколько, это цепочка: последнее в списке брошено первым, \
оно и есть исходная причина.
- likely_cause — вероятная причина с опорой на конкретные фреймы, до 600 \
символов. Уложись в этот предел: продолжение не поместится в сообщение и будет \
обрезано на полуслове. Разбей текст на 2–3 коротких абзаца, разделив их пустой \
строкой: сплошную стену текста в комментарии никто не читает.
- checks — от 2 до 4 пунктов «что проверить или поправить», в порядке \
убывания полезности. Каждый пункт — одно предложение до 200 символов: это \
список зацепок, а не инструкция.

Правила:
- Только простой текст. Никакого markdown: ни **, ни обратных кавычек, ни \
таблиц, ни нумерации — разметку соберёт получатель, твои символы разметки \
он покажет как есть.
- У каждого фрейма указан владелец: DEVELOPER — код команды, THIRD_PARTY и \
VENDOR — подключённые библиотеки, PLATFORM, RUNTIME и SYSTEM — ОС и рантайм. \
Опирайся сначала на DEVELOPER, потом на границу между библиотекой и кодом \
команды: чужой код почти никогда не причина сам по себе, но показывает, какой \
контракт нарушен.
- Если фреймов DEVELOPER нет вовсе, а имена выглядят как `q2.f.R` и файлы как \
`SourceFile` — код обфусцирован (R8/ProGuard), и символы не восстановлены. \
Тогда НЕ угадывай, какой это был класс. Опирайся на типы и сообщения \
исключений, на имена библиотек и на порядок вызовов, и прямо скажи, что \
стектрейс обфусцирован и для точного места нужен mapping-файл.
- Не выдумывай имён файлов, классов, методов и номеров строк, которых нет во \
входных данных. Лучше признать нехватку данных, чем дать правдоподобную ложь: \
по этому разбору человек пойдёт править код.
- Пиши по-русски, технические имена оставляй как есть."""


class AnalysisError(Exception):
    """Ответ модели не годится для комментария."""


@dataclass(frozen=True)
class Verdict:
    what_happened: str
    likely_cause: str
    checks: tuple[str, ...]


# --- выбор того, что вообще разбирать --------------------------------------

def _blamed_exception(event: CrashEvent):
    """Виноватое исключение, иначе первое.

    В `exceptions` вложенные идут в обратном хронологическом порядке, и
    последняя запись — первое брошенное. Признак `blamed` ставит анализатор
    Crashlytics, и он точнее любой нашей эвристики по порядку.
    """
    for item in event.exceptions:
        if item.blamed:
            return item
    return event.exceptions[0] if event.exceptions else None


def _blamed_thread(event: CrashEvent):
    for item in event.threads:
        if item.blamed:
            return item
    return event.threads[0] if event.threads else None


def _primary_frames(event: CrashEvent) -> tuple[EventFrame, ...]:
    """Стек, по которому и идёт разбор: Java-исключение либо упавший поток."""
    exception = _blamed_exception(event)
    if exception and exception.frames:
        return exception.frames
    thread = _blamed_thread(event)
    return thread.frames if thread else ()


def headline(event: CrashEvent, row: IssueRow) -> str:
    """Короткое имя класса исключения для заголовка комментария.

    Имя берётся из события, а не из `title` issue: в title лежит фрейм
    (часто обфусцированный), и класса там нет — та же причина, по которой
    `digest._issue_cell` читает только subtitle.
    """
    exception = _blamed_exception(event)
    name = exception.type if exception and exception.type else row.subtitle.partition(" - ")[0]
    parts = [part for part in name.split(".") if part]
    if not parts:
        return name or "краш"

    # У обфусцированного класса последний сегмент — одна-две буквы, и как
    # заголовок он не говорит ничего: `z` против `protobuf.z`, где видно хотя
    # бы библиотеку. Необфусцированному имени лишний сегмент только мешает,
    # поэтому пакет добавляется ровно в этом случае.
    if len(parts[-1]) <= 2 and len(parts) > 1:
        return ".".join(parts[-2:])
    return parts[-1]


# --- промпт ----------------------------------------------------------------

def _frame_lines(frames: Sequence[EventFrame]) -> list[str]:
    """Пронумерованные фреймы с пометкой владельца.

    При переполнении шапки сохраняются ВСЕ фреймы DEVELOPER, даже лежащие
    глубоко: именно они — код команды, и потерять их ради чужих
    Looper.loop/ZygoteInit значило бы выбросить самое полезное.
    """
    if len(frames) <= MAX_FRAMES:
        kept = list(enumerate(frames, 1))
    else:
        kept = list(enumerate(frames[:MAX_FRAMES], 1))
        kept += [
            (index, frame)
            for index, frame in enumerate(frames[MAX_FRAMES:], MAX_FRAMES + 1)
            if frame.owner == "DEVELOPER"
        ]

    lines = [
        f"  {index:>3}. {frame.where} [{frame.owner}]"
        + ("  <-- виноват" if frame.blamed else "")
        for index, frame in kept
    ]
    hidden = len(frames) - len(kept)
    if hidden:
        lines.append(f"  … ещё {hidden} фреймов не из кода команды опущено")
    return lines


def build_prompt(event: CrashEvent, row: IssueRow) -> str:
    """Текстовый дамп события для модели. Чистая функция."""
    parts: list[str] = [
        f"Issue: {row.subtitle or row.title}",
        f"Тип события: {row.error_type}",
        f"Событий: {row.events_count}, затронуто пользователей: {row.impacted_users}",
        f"Известен с версии: {row.first_seen_version}, "
        f"последняя с ним: {row.last_seen_version}",
        "",
    ]

    meta = [
        ("Платформа", event.platform),
        ("Версия приложения", event.app_version),
        ("Устройство", event.device),
        ("ОС", event.operating_system),
        ("Память", event.memory),
        ("Состояние процесса", event.process_state),
        ("Ориентация", event.orientation),
        ("Время события", event.event_time),
    ]
    parts += [f"{label}: {value}" for label, value in meta if value]

    if event.blame_frame:
        parts += [
            "",
            f"Фрейм, назначенный виноватым анализатором Crashlytics: "
            f"{event.blame_frame.where} [{event.blame_frame.owner}]",
        ]

    for index, exception in enumerate(event.exceptions, 1):
        tag = " (виноватое)" if exception.blamed else ""
        tag += " (вложенное)" if exception.nested else ""
        head = f"{exception.type}: {exception.message}" if exception.message else exception.type
        parts += [
            "",
            f"Исключение {index} из {len(event.exceptions)}{tag}: {head}",
            f"Стек ({len(exception.frames)} фреймов):",
        ]
        parts += _frame_lines(exception.frames)

    # Потоки нужны там, где `exceptions` пуст: native-краши и ANR. Для
    # обычного java-исключения стек уже выведен выше, и повторять его незачем.
    if not event.exceptions:
        for thread in event.threads:
            signal = f", сигнал {thread.signal}" if thread.signal else ""
            parts += [
                "",
                f"Упавший поток {thread.name or '?'}{signal} "
                f"({len(thread.frames)} фреймов):",
            ]
            parts += _frame_lines(thread.frames)

    if event.breadcrumbs:
        parts += ["", "События Analytics перед падением (от старых к новым):",
                  "  " + " → ".join(event.breadcrumbs)]
    if event.logs:
        parts += ["", "Логи приложения перед падением:"]
        parts += [f"  {line}" for line in event.logs]
    if event.custom_keys:
        parts += ["", "Custom keys:"]
        parts += [f"  {key} = {value}" for key, value in event.custom_keys]
    if event.user:
        parts += ["", f"Идентификатор пользователя: {event.user}"]

    return "\n".join(parts)


# --- ответ модели ----------------------------------------------------------

def _field(payload: dict, key: str, limit: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    return md.short(" ".join(value.split()), limit)


def parse_answer(payload: dict) -> Verdict:
    """Проверяет форму ответа и усекает поля до разумных длин."""
    what = _field(payload, "what_happened", LIMIT_WHAT)
    cause = _field(payload, "likely_cause", LIMIT_CAUSE)

    raw = payload.get("checks")
    checks: tuple[str, ...] = ()
    if isinstance(raw, list):
        checks = tuple(
            md.short(" ".join(str(item).split()), LIMIT_CHECK)
            for item in raw[:MAX_CHECKS]
            if str(item).strip()
        )

    if not what and not cause:
        raise AnalysisError(
            "в ответе модели нет ни what_happened, ни likely_cause: "
            f"ключи {sorted(payload)[:10]}"
        )
    return Verdict(what_happened=what, likely_cause=cause, checks=checks)


# --- рендер комментария ----------------------------------------------------

def _paragraph(text: str) -> str:
    """Многострочный текст внутри абзаца.

    Перевод строки собирается тегом <br>: `md.esc` схлопывает \\n в пробел
    намеренно (см. его докстринг), поэтому разбивать надо до экранирования.

    Пустая строка в ответе модели сохраняется как <br><br>, а не схлопывается:
    600 символов причины одним куском — нечитаемая стена, и разделение на
    абзацы единственное, чем её можно разбить, не трогая сам текст.
    """
    blocks = [
        "<br>".join(md.esc(line) for line in block.splitlines() if line.strip())
        for block in re.split(r"\n\s*\n", text.strip())
    ]
    return "<br><br>".join(block for block in blocks if block)


def render_comment(verdict: Verdict, event: CrashEvent, row: IssueRow,
                   *, project: str, app_id: str, model: str = "") -> str:
    """Комментарий с разбором. Разметку собираем МЫ, не модель.

    Модель отдаёт плоский текст, и он целиком проходит через `md.esc`. Иначе
    пришлось бы выбирать между двумя плохими вариантами: экранировать её
    markdown (убив разметку) или пустить неэкранированным (а там текст краша
    с `<init>`, `$`, `#` и скобками — Telegram не вернёт ошибку, он вернёт
    правдоподобно испорченное сообщение, см. `markdown.esc`).
    """
    name = md.link(
        md.esc(md.short(headline(event, row), 40)),
        issue_url(project, app_id, row.issue_id),
    )
    parts = [f"# {name}", ""]

    if verdict.what_happened:
        parts += [f"### {SECTIONS['what']}", _paragraph(verdict.what_happened), ""]

    frames = _table_frames(event)
    if frames:
        parts += [
            f"### {SECTIONS['where']}",
            "",
            md.table(
                ("Фрейм", "Чей"),
                [[md.esc(md.short(_cell(f), 46)), md.esc(f.owner)] for f in frames],
                "ll",
            ),
            "",
        ]

    if verdict.likely_cause:
        parts += [f"### {SECTIONS['cause']}",
                  _paragraph(verdict.likely_cause), ""]

    if verdict.checks:
        # Пункты склеены <br><br>, а не переводами строк: нумерованный список
        # в этом формате не гарантирован, а тег даёт ровно те же строки при
        # любом поведении парсера. Двойной — чтобы пункты по 200 символов не
        # слипались в один абзац.
        items = "<br><br>".join(
            f"**{md.esc(f'{index}.')}** {md.esc(text)}"
            for index, text in enumerate(verdict.checks, 1)
        )
        parts += [f"### {SECTIONS['checks']}", items, ""]

    parts += [f"### {SECTIONS['context']}", "",
              _context_table(event, row, model)]
    return "\n".join(parts)


def _cell(frame: EventFrame) -> str:
    """Содержимое ячейки «Фрейм»: место, по которому открывают редактор.

    Имя символа урезает `_symbol` по бюджету, имя файла печатается только
    когда что-то добавляет (см. ниже). В промпт при этом уходит полное имя
    фрейма: там ширины нет и ужимать нечего.
    """
    symbol = _symbol(frame.symbol)
    file = "" if frame.file in PLACEHOLDER_FILES else frame.file
    stem = file.rsplit(".", 1)[0] if file else ""
    cls = symbol.split(".")[-2] if "." in symbol else symbol

    # Имя файла не печатается в двух случаях: это заглушка вроде `SourceFile`
    # или оно повторяет класс (ProfileFragment.kt у ProfileFragment.onResume).
    # На 46 символов ячейки повторять одно слово дважды — непозволительно.
    if symbol and (not file or stem == cls):
        return f"{symbol}:{frame.line}" if frame.line else symbol

    place = ":".join(part for part in (file, frame.line) if part)
    if symbol and place:
        return f"{symbol} ({place})"
    return symbol or place or frame.library


def _symbol(symbol: str) -> str:
    """Хвост имени: сколько сегментов влезает в бюджет, но не меньше двух.

    Для обычного кода это `ProfileFragment.onResume` — пакет только мешает.
    Для обфусцированного пакет, наоборот, единственное осмысленное, что во
    фрейме осталось: `preferences.protobuf.h$c.B` вместо `h$c.B`, где и класс,
    и метод — по одной букве.
    """
    parts = [part for part in symbol.split(".") if part]
    if not parts:
        return ""

    kept = parts[-2:]
    for count in range(3, len(parts) + 1):
        candidate = parts[-count:]
        if len(".".join(candidate)) > SYMBOL_BUDGET:
            break
        kept = candidate
    return ".".join(kept)


def _table_frames(event: CrashEvent) -> tuple[EventFrame, ...]:
    """Фреймы для таблицы: сначала виноватый, потом код команды, потом прочее.

    Порядок именно такой, потому что читающий ищет в таблице ответ на вопрос
    «куда мне идти в своём коде», а не копию стека.
    """
    chosen: list[EventFrame] = []
    seen: set[tuple[str, str, str]] = set()

    def add(frame: EventFrame) -> None:
        key = (frame.symbol, frame.file, frame.line)
        if key not in seen and (frame.symbol or frame.file):
            seen.add(key)
            chosen.append(frame)

    frames = _primary_frames(event)
    if event.blame_frame:
        add(event.blame_frame)
    for frame in frames:
        if frame.blamed:
            add(frame)
    for frame in frames:
        if frame.owner == "DEVELOPER":
            add(frame)
    # Второй ярус — подключённые библиотеки. Он нужен обфусцированным сборкам,
    # где фреймов DEVELOPER не бывает вовсе: без него таблица заполнялась бы
    # фреймами ОС (`Looper.loop`, `ZygoteInit.main`), одинаковыми у всех
    # падений на свете и потому бесполезными.
    for frame in frames:
        if frame.owner in ("THIRD_PARTY", "VENDOR"):
            add(frame)
    for frame in frames:
        add(frame)
    return tuple(chosen[:TABLE_ROWS])


def _os(event: CrashEvent) -> str:
    """ОС читаемо.

    `operatingSystem.displayName` у боевого приложения приходит как
    `Unknown (13)`: имя ОС Google не определил, версию определил. Платформа при
    этом известна точно, и собрать `Android 13` из двух достоверных полей
    честнее, чем печатать `Unknown` — выдумки здесь нет, оба значения пришли
    из события.
    """
    name = event.operating_system
    if name and not name.startswith("Unknown"):
        return name
    platform = event.platform.capitalize() if event.platform else ""
    if platform and event.os_version:
        return f"{platform} {event.os_version}"
    return platform or name


def _context_table(event: CrashEvent, row: IssueRow, model: str) -> str:
    """Обстановка краша таблицей «метка — значение».

    Той же формы, что таблица в стартовом сообщении: перечисление через
    запятую одной строкой читалось как хвост предыдущего абзаца, а не как
    отдельный блок. Пустые поля строк не создают — пустая метка хуже
    отсутствующей.
    """
    rows = []
    if event.app_version:
        rows.append(["📦 Версия", md.esc(event.app_version)])
    if event.device:
        rows.append(["📱 Устройство", md.esc(event.device)])
    system = _os(event)
    if system:
        rows.append(["🖥 ОС", md.esc(system)])
    rows.append([
        "📊 Влияние",
        md.esc(
            f"{row.events_count} "
            f"{plural(row.events_count, 'событие', 'события', 'событий')} / "
            f"{row.impacted_users} "
            f"{plural(row.impacted_users, 'юзер', 'юзера', 'юзеров')}"
        ),
    ])
    if event.memory:
        rows.append(["🧠 Память", md.esc(event.memory)])
    if model:
        # В обратных кавычках и без md.esc — как расписание и модель в
        # стартовом сообщении: внутри code-span разметки нет, а экранирующий
        # слэш показался бы получателю слэшем.
        rows.append(["🤖 Модель", f"`{model}`"])
    return md.table(("", ""), rows, "ll")
