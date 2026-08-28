from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from crashdigest.crashlytics import IssueRow
from crashdigest.digest import (
    MAX_LISTED,
    TELEGRAM_LIMIT,
    issue_url,
    plural,
    render,
    render_bootstrap,
    render_error,
)

START = datetime(2026, 8, 25, 20, 24, tzinfo=timezone.utc)
END = datetime(2026, 8, 26, 20, 24, tzinfo=timezone.utc)
ARGS = dict(app_name="Example", project="example-prod", app_id="app-1")
VERSIONS = ["5.15.0", "5.14.0", "5.13.1"]


def row(issue_id="a", subtitle="java.lang.IllegalStateException - boom", events=18, users=16):
    return IssueRow(
        issue_id=issue_id,
        title="SourceFile - x.y",
        subtitle=subtitle,
        error_type="FATAL",
        state="OPEN",
        last_seen_version="5.11.5",
        first_seen_version="5.0.0",
        events_count=events,
        impacted_users=users,
    )


def test_plural_russian_rules():
    assert plural(1, "событие", "события", "событий") == "событие"
    assert plural(3, "событие", "события", "событий") == "события"
    assert plural(18, "событие", "события", "событий") == "событий"
    assert plural(21, "событие", "события", "событий") == "событие"
    assert plural(112, "событие", "события", "событий") == "событий"


def test_issue_url_uses_console_template():
    url = issue_url("example-prod", "app-1", "abc")
    assert url == (
        "https://console.firebase.google.com/v1/appid/project/example-prod"
        "/crashlytics/app/app-1/issues/abc"
    )


def test_render_includes_class_message_metrics_and_link():
    [text] = render([row()], START, END, **ARGS)
    assert "Example" in text
    assert "java.lang.IllegalStateException" in text
    assert "boom" in text
    assert "18 событий" in text
    assert "16 юзеров" in text
    assert "/issues/a" in text


def test_render_omits_message_line_when_subtitle_has_no_separator():
    [text] = render([row(subtitle="android.os.DeadSystemException")], START, END, **ARGS)
    assert "android.os.DeadSystemException" in text
    assert " - " not in text


def test_render_escapes_html():
    [text] = render(
        [row(subtitle="java.lang.IllegalStateException - Apply on <node> a&b")],
        START, END, **ARGS,
    )
    assert "&lt;node&gt;" in text
    assert "a&amp;b" in text


def test_render_reports_no_new_crashes():
    [text] = render([], START, END, **ARGS)
    assert "нет" in text.lower()


def test_render_warns_when_window_truncated():
    [text] = render([row()], START, END, truncated=True, **ARGS)
    assert "разрыв" in text.lower()


def test_period_prints_both_ends_in_same_timezone():
    """Настоящее суточное окно: start и end ровно в 24 часах друг от друга по
    UTC, но start пришёл как UTC, а end — в MSK (UTC+3). До фикса это
    печаталось как «25.08 07:00 → 26.08 10:00» — на вид 27 часов.
    """
    msk = ZoneInfo("Europe/Moscow")
    start_utc = datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc)
    end_msk = datetime(2026, 8, 26, 10, 0, tzinfo=msk)  # = 26.08 07:00 UTC

    real_span_hours = (end_msk - start_utc).total_seconds() / 3600
    assert real_span_hours == 24  # действительно суточное окно

    [text] = render([row()], start_utc, end_msk, **ARGS)

    assert "25.08 10:00" in text   # start, напечатанный в зоне end (MSK)
    assert "26.08 10:00" in text   # end как есть
    assert "07:00" not in text     # больше не смешиваем зоны на печати


def test_render_splits_long_output_under_limit():
    # MAX_LISTED теперь ограничивает перечисление 30 позициями (finding #6),
    # поэтому здесь используется 25 строк (< MAX_LISTED) — проверяем именно
    # нарезку по лимиту Telegram, а не обрезание списка (это отдельный тест
    # ниже).
    rows = [row(issue_id=f"id{i}", subtitle=f"cls{i} - " + "x" * 150) for i in range(25)]
    parts = render(rows, START, END, **ARGS)
    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    assert "id24" in "".join(parts)


def test_render_truncates_class_without_separator_to_bounded_message():
    huge_subtitle = "x" * 9000  # без " - ", весь текст ушёл бы в class
    parts = render([row(subtitle=huge_subtitle)], START, END, **ARGS)
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)


def test_render_bounds_listed_issues_and_notes_omitted_count():
    rows = [row(issue_id=f"id{i}", subtitle=f"cls{i} - msg{i}") for i in range(3000)]
    parts = render(rows, START, END, **ARGS)
    text = "".join(parts)

    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    assert len(parts) < 10                       # число сообщений ограничено
    assert "id29" in text                         # 30-я (последняя из показанных)
    assert "id30" not in text                     # 31-я уже не перечисляется
    assert f"и ещё {3000 - MAX_LISTED}" in text    # сколько именно опущено
    assert "консоль Crashlytics" in text


def test_render_reports_scanned_count_when_no_new_crashes():
    [text] = render([], START, END, scanned=71, **ARGS)
    assert "новых падений нет" in text.lower()
    assert "71" in text
    assert "известн" in text


def test_render_bootstrap_mentions_count():
    [text] = render_bootstrap(412, "Example")
    assert "412" in text


def test_render_error_is_single_message():
    parts = render_error("Crashlytics недоступен", "HTTP 403")
    assert len(parts) == 1
    assert "403" in parts[0]


def test_render_lists_tracked_versions_when_empty():
    [text] = render([], START, END, scanned=46, versions=VERSIONS, **ARGS)
    assert "5.15.0, 5.14.0, 5.13.1" in text
    assert "46 известных" in text


def test_render_lists_tracked_versions_when_not_empty():
    [text] = render([row()], START, END, versions=VERSIONS, **ARGS)
    assert "на версиях 5.15.0, 5.14.0, 5.13.1" in text


def test_render_omits_versions_line_when_not_given():
    [text] = render([row()], START, END, **ARGS)
    assert "на версиях" not in text


def test_render_no_longer_prints_issue_version():
    """lastSeenVersion глобален и противоречит шапке про фильтр."""
    [text] = render([row()], START, END, versions=VERSIONS, **ARGS)
    assert "версия 5.11.5" not in text
    assert "18 событий" in text
    assert "16 юзеров" in text


from crashdigest.digest import (
    Growth,
    WeekTotals,
    render_weekly,
    week_totals,
    weekly_growth,
)

PREV_WEEK = (START - timedelta(days=7), START)
CUR_WEEK = (START, END + timedelta(days=6))


def irow(issue_id, events, users=1, subtitle="cls - msg", first="5.12.0"):
    return IssueRow(
        issue_id=issue_id,
        title="t",
        subtitle=subtitle,
        error_type="FATAL",
        state="OPEN",
        last_seen_version="5.14.0",
        first_seen_version=first,
        events_count=events,
        impacted_users=users,
    )


def test_week_totals_sums_events_and_users():
    totals = week_totals([irow("a", 10, 4), irow("b", 5, 3)])
    assert totals == WeekTotals(events=15, users=7)


def test_growth_keeps_only_rising_issues():
    cur = [irow("a", 64), irow("b", 5), irow("c", 7)]
    prev = [irow("a", 20), irow("b", 9)]
    growth = weekly_growth(cur, prev)
    assert [g.row.issue_id for g in growth] == ["a", "c"]


def test_growth_sorted_by_absolute_delta():
    cur = [irow("small", 22), irow("big", 64)]
    prev = [irow("small", 2), irow("big", 20)]
    growth = weekly_growth(cur, prev)
    assert [g.row.issue_id for g in growth] == ["big", "small"]


def test_issue_absent_last_week_counts_as_growth_from_zero():
    growth = weekly_growth([irow("new", 7)], [])
    assert growth == [Growth(row=growth[0].row, was_events=0)]
    assert growth[0].was_events == 0


def test_issue_gone_this_week_is_not_reported():
    assert weekly_growth([], [irow("old", 40)]) == []


def test_render_weekly_header_shows_both_periods_and_totals():
    [text] = render_weekly(
        weekly_growth([irow("a", 64)], [irow("a", 20)]),
        versions=VERSIONS,
        previous=PREV_WEEK,
        current=CUR_WEEK,
        prev_totals=WeekTotals(events=1049, users=266),
        cur_totals=WeekTotals(events=398, users=304),
        top=5,
        **ARGS,
    )
    assert "1049 → 398" in text
    assert "-651" in text
    assert "266 → 304" in text
    assert "+38" in text
    assert "5.15.0, 5.14.0, 5.13.1" in text
    assert text.count("→") >= 3  # два периода в шапке и стрелки в числах


def test_render_weekly_uses_down_arrow_when_events_fell():
    [text] = render_weekly(
        [], versions=VERSIONS, previous=PREV_WEEK, current=CUR_WEEK,
        prev_totals=WeekTotals(1049, 266), cur_totals=WeekTotals(398, 304),
        top=5, **ARGS,
    )
    assert text.startswith("📉")


def test_render_weekly_uses_up_arrow_when_events_grew():
    [text] = render_weekly(
        [], versions=VERSIONS, previous=PREV_WEEK, current=CUR_WEEK,
        prev_totals=WeekTotals(200, 100), cur_totals=WeekTotals(350, 180),
        top=5, **ARGS,
    )
    assert text.startswith("📈")


def test_render_weekly_says_so_when_nothing_grows():
    [text] = render_weekly(
        [], versions=VERSIONS, previous=PREV_WEEK, current=CUR_WEEK,
        prev_totals=WeekTotals(1049, 266), cur_totals=WeekTotals(398, 304),
        top=5, **ARGS,
    )
    assert "Растущих нет" in text


def test_render_weekly_issue_line_says_known_since_and_delta():
    [text] = render_weekly(
        weekly_growth([irow("a", 64, 55)], [irow("a", 20, 17)]),
        versions=VERSIONS, previous=PREV_WEEK, current=CUR_WEEK,
        prev_totals=WeekTotals(100, 50), cur_totals=WeekTotals(120, 60),
        top=5, **ARGS,
    )
    assert "известен с" in text
    assert "стреляет с" not in text
    assert "было 20 → стало 64" in text
    assert "55 юзеров" in text
    assert "+44" in text
    assert "новая версия" not in text


def test_render_weekly_limits_to_top():
    growth = weekly_growth([irow(f"id{i}", 100 - i) for i in range(9)], [])
    parts = render_weekly(
        growth, versions=VERSIONS, previous=PREV_WEEK, current=CUR_WEEK,
        prev_totals=WeekTotals(0, 0), cur_totals=WeekTotals(900, 9),
        top=3, **ARGS,
    )
    text = "\n".join(parts)
    assert "3 из 9" in text
    assert "id3" not in text


def test_render_weekly_stays_within_telegram_limit():
    growth = weekly_growth([irow(f"id{i}", 100 - i, subtitle="cls - " + "x" * 300)
                            for i in range(30)], [])
    parts = render_weekly(
        growth, versions=VERSIONS, previous=PREV_WEEK, current=CUR_WEEK,
        prev_totals=WeekTotals(0, 0), cur_totals=WeekTotals(1, 1),
        top=30, **ARGS,
    )
    assert all(len(p) <= TELEGRAM_LIMIT for p in parts)
