"""Сборка текста сообщения. Без сети и диска — чистые функции."""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from crashdigest.crashlytics import IssueRow
from crashdigest import markdown as md

CONSOLE = "https://console.firebase.google.com/v1/appid/project"

# Жёсткий лимит rich-сообщения 32768; берём с запасом на аномально длинные
# имена классов вроде RemoteServiceException$CannotDeliverBroadcastException.
BUDGET = 30000

WEEKLY_TAIL = 20


def _tg_len(text: str) -> int:
    """Длина в кодовых единицах UTF-16 — так же, как исторически считает
    длину сообщения Telegram (лимиты и offset'ы сущностей). Python `len()`
    считает кодовые точки: не-BMP символы (эмодзи вроде 🔥) стоят 1 у нас и
    2 у них, и бюджет, посчитанный в кодовых точках, может пропустить
    сообщение, реально превышающее жёсткий лимит.
    """
    return len(text.encode("utf-16-le")) // 2


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по правилам 1 / 2-4 / 5+."""
    if n % 100 in (11, 12, 13, 14):
        return many
    tail = n % 10
    if tail == 1:
        return one
    if tail in (2, 3, 4):
        return few
    return many


def issue_url(project: str, app_id: str, issue_id: str) -> str:
    return f"{CONSOLE}/{project}/crashlytics/app/{app_id}/issues/{issue_id}"


def _split_subtitle(subtitle: str) -> tuple[str, str]:
    """Делит 'Класс - текст' на части. Разделителя может не быть вовсе."""
    cls, sep, message = subtitle.partition(" - ")
    return (cls, message) if sep else (subtitle, "")


def _fmt_period(start: datetime, end: datetime) -> str:
    """Печатает оба конца окна в одной зоне — зоне `end`.

    `start` приходит из БД нормализованным в UTC, `end` — это `now` в
    локальной зоне. Печать «как есть» даёт разные зоны на разных концах
    строки: настоящее суточное окно превращалось в «25.08 07:00 → 26.08
    10:00», что читается как 27 часов.
    """
    start = start.astimezone(end.tzinfo)
    return f"{start.strftime('%d.%m %H:%M')} → {end.strftime('%d.%m %H:%M')}"


def _head(versions: Sequence[str] | None, period: str) -> str:
    """Шапка двумя строками. <br>, а не \\n: внутри одного абзаца перевод
    строки делается тегом, иначе Telegram склеит метки в одну строку.
    """
    lines = []
    if versions:
        lines.append(f"📦 Версии: **{md.esc(', '.join(versions))}**")
    lines.append(f"🗓 Период: {period}")
    return "<br>".join(lines)


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _issue_cell(row: IssueRow, project: str, app_id: str, msg_limit: int) -> str:
    """Имя класса — ссылка на issue, под ним текст ошибки.

    Класс берётся ТОЛЬКО из subtitle: в title лежит обфусцированный фрейм
    вида `SourceFile - B1.g$a$a.a`, и rsplit по нему даёт мусор.
    """
    cls, message = _split_subtitle(row.subtitle)
    name = md.link(
        md.esc(_short(cls.rsplit(".", 1)[-1] or cls, 30)),
        issue_url(project, app_id, row.issue_id),
    )
    if message and msg_limit:
        return f"{name}<br>{md.esc(_short(message, msg_limit))}"
    return name


def render(
    rows: Sequence[IssueRow],
    start: datetime,
    end: datetime,
    *,
    app_name: str,
    project: str,
    app_id: str,
    scanned: int | None = None,
    versions: Sequence[str] | None = None,
) -> list[str]:
    head = _head(versions, _fmt_period(start, end))
    name = md.esc(app_name)

    if not rows:
        # «Новых падений нет» неотличимо от «фильтр сломан и вернул 0 строк».
        # Печатаем срез и сколько issue в нём известно.
        lines = [f"# ✅ {name} — новых падений нет", "", head]
        if scanned is not None:
            lines.append(f"<br>📊 Известных issue: **{scanned}**")
        return ["\n".join(lines)]

    top = [f"# 🔴 Новые падения {name} — {len(rows)}", "", head, ""]
    used = _tg_len("\n".join(top))

    # Колонки типа события нет: при ERROR_TYPES=FATAL она повторяла одно и
    # то же в каждой строке. ВНИМАНИЕ: если будет включён ANR, колонку надо
    # вернуть — иначе падения и зависания смешаются неразличимо.
    header = ("Ошибка", "События", "Юзеров")

    # Account for table header and separator lines
    used += _tg_len("| Ошибка | События | Юзеров |") + 1 + _tg_len("|:--|--:|--:|")

    cells: list[list[str]] = []
    omitted = 0
    # Reserve space for "… и ещё N issue — см. консоль Crashlytics" message if needed
    truncation_buffer = 100

    for index, row in enumerate(rows):
        cell = [
            _issue_cell(row, project, app_id, 48),
            f"**{row.events_count}**",
            str(row.impacted_users),
        ]
        size = sum(_tg_len(c) for c in cell) + 10 + 1  # разделители, перевод строки и newline после строки
        if used + size + truncation_buffer > BUDGET:
            omitted = len(rows) - index
            break
        cells.append(cell)
        used += size

    parts = top + [md.table(header, cells, "lrr")]
    if omitted:
        parts += [
            "",
            f"… и ещё **{omitted}** {plural(omitted, 'issue', 'issue', 'issue')} "
            "— см. консоль Crashlytics",
        ]
    return ["\n".join(parts)]


def render_bootstrap(count: int, app_name: str,
                     versions: Sequence[str] | None = None) -> list[str]:
    lines = [
        f"# 🧭 {md.esc(app_name)} — инициализация",
        "",
        f"Запомнено **{count}** {plural(count, 'issue', 'issue', 'issue')}, "
        "дальше сообщаю только о новых.",
    ]
    if versions:
        lines += ["", f"📦 Версии: **{md.esc(', '.join(versions))}**"]
    return ["\n".join(lines)]


def render_error(headline: str, detail: str) -> list[str]:
    """Единственная форма на HTML: уходит через sendMessage, потому что
    обязана дойти, когда сломалось всё остальное, включая сам rich.
    """
    return [
        f"⚠️ <b>{html.escape(headline)}</b>\n"
        f"<code>{html.escape(detail[:600])}</code>\n"
        "Состояние не сдвинуто — падения не потеряются, придут следующим прогоном."
    ]


@dataclass(frozen=True)
class Growth:
    row: IssueRow
    was_events: int

    @property
    def delta(self) -> int:
        return self.row.events_count - self.was_events


@dataclass(frozen=True)
class WeekTotals:
    events: int
    users: int


def week_totals(rows: Sequence[IssueRow]) -> WeekTotals:
    """Суммы за неделю.

    Пользователи складываются по issue, а не считаются как различные: API
    отдаёт impactedUsersCount только в разбивке по issue, и человек, упавший
    в трёх issue, посчитан трижды. Как абсолют цифра завышена, как сравнение
    двух недель корректна — перекос одинаков в обоих числах.
    """
    return WeekTotals(
        events=sum(r.events_count for r in rows),
        users=sum(r.impacted_users for r in rows),
    )


def weekly_growth(
    current: Sequence[IssueRow], previous: Sequence[IssueRow]
) -> list[Growth]:
    """Растущие issue, от большего прироста к меньшему.

    Ранжирование по АБСОЛЮТНОМУ приросту: рост с 2 до 20 даёт десятикратное
    отношение и вытеснил бы рост с 20 до 64, который затрагивает на два
    порядка больше людей.
    """
    was = {r.issue_id: r.events_count for r in previous}
    growth = [
        Growth(row=r, was_events=was.get(r.issue_id, 0))
        for r in current
        if r.events_count > was.get(r.issue_id, 0)
    ]
    growth.sort(key=lambda g: g.delta, reverse=True)
    return growth


def _fmt_week(period: tuple[datetime, datetime]) -> str:
    start, end = period
    start = start.astimezone(end.tzinfo)
    return f"{start.strftime('%d.%m')}–{end.strftime('%d.%m')}"


def _delta(was: int, now: int) -> str:
    diff = now - was
    return f"+{diff}" if diff > 0 else str(diff)


def render_weekly(
    growth: Sequence[Growth],
    *,
    app_name: str,
    project: str,
    app_id: str,
    versions: Sequence[str],
    previous: tuple[datetime, datetime],
    current: tuple[datetime, datetime],
    prev_totals: WeekTotals,
    cur_totals: WeekTotals,
    top: int,
) -> list[str]:
    arrow = "📈" if cur_totals.events > prev_totals.events else "📉"
    period = f"{_fmt_week(previous)} → {_fmt_week(current)}"
    parts = [
        f"# {arrow} Фон за неделю — {md.esc(app_name)}",
        "",
        _head(versions, period),
        "",
        md.table(
            ("", "было", "стало", "Δ"),
            [
                ["события", str(prev_totals.events), f"**{cur_totals.events}**",
                 _delta(prev_totals.events, cur_totals.events)],
                ["юзеров", str(prev_totals.users), f"**{cur_totals.users}**",
                 _delta(prev_totals.users, cur_totals.users)],
            ],
            "lrrr",
        ),
    ]

    if not growth:
        return ["\n".join(parts + ["", "Растущих нет — фон не увеличивается."])]

    listed = list(growth[:top])
    parts += ["", f"### 🔥 Растут сильнее всего — {len(listed)} из {len(growth)}", ""]
    parts.append(md.table(
        ("Ошибка", "Известен с", "Было", "Стало", "Δ", "Юзеров"),
        [
            [
                _issue_cell(item.row, project, app_id, 44),
                # «известен с», а не «стреляет с»: firstSeenVersion глобален
                # и означает «известен с незапамятных времён».
                md.esc(item.row.first_seen_version or "—"),
                str(item.was_events),
                f"**{item.row.events_count}**",
                f"**+{item.delta}**",
                str(item.row.impacted_users),
            ]
            for item in listed
        ],
        "llrrrr",
    ))

    tail = list(growth[top:top + WEEKLY_TAIL])
    if tail:
        # В раскрывашке только имя-ссылка и числа: с текстом ошибки
        # свёрнутый блок сам становится простынёй.
        parts += [
            "",
            f"<details><summary>📂 Ещё {len(tail)} растущих</summary>",
            "",
            md.table(
                ("Ошибка", "Было", "Стало", "Δ"),
                [
                    [_issue_cell(i.row, project, app_id, 0), str(i.was_events),
                     str(i.row.events_count), f"+{i.delta}"]
                    for i in tail
                ],
                "lrrr",
            ),
            "",
            "</details>",
        ]
    return ["\n".join(parts)]
