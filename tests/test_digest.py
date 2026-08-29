from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from crashdigest.crashlytics import IssueRow
from crashdigest.digest import (
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


def row(issue_id="a", title="SourceFile - x.y", subtitle="java.lang.IllegalStateException - boom", events=18, users=16, last_seen_version="5.11.5", first_seen_version="5.0.0"):
    return IssueRow(
        issue_id=issue_id,
        title=title,
        subtitle=subtitle,
        error_type="FATAL",
        state="OPEN",
        last_seen_version=last_seen_version,
        first_seen_version=first_seen_version,
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


def test_daily_header_has_two_labelled_lines():
    out = render([row("a")], START, END, app_name="Example", project="p",
                 app_id="1:2:android:3", versions=["5.15.0", "5.14.0"])[0]
    assert "# 🔴 Новые падения Example — 1" in out
    assert "📦 Версии: **5\\.15\\.0, 5\\.14\\.0**" in out
    assert "🗓 Период: " in out
    assert "<br>" in out, "метки обязаны стоять друг под другом, а не в одну строку"


def test_daily_lists_every_issue_without_a_cap():
    """MAX_LISTED=30 снят: при лимите 32768 прятать нечего."""
    rows = [row(f"i{n}") for n in range(45)]
    out = render(rows, START, END, app_name="Example", project="p",
                 app_id="1:2:android:3", versions=["5.15.0"])[0]
    assert out.count("| [") == 45
    assert "см. консоль" not in out


def test_daily_truncates_by_length_not_by_count():
    """Обрезка — признак аварии, а не обычного дня, и считается по символам."""
    long_subtitle = "java.lang.Boom - " + "x" * 400
    rows = [row(f"i{n}", subtitle=long_subtitle) for n in range(400)]
    out = render(rows, START, END, app_name="Example", project="p",
                 app_id="1:2:android:3", versions=["5.15.0"])[0]
    assert len(out) <= 30000
    assert "см. консоль Crashlytics" in out


def test_daily_truncation_counts_utf16_units_not_codepoints():
    """Telegram считает длину сообщения в кодовых единицах UTF-16, а не в
    кодовых точках Python. Не-BMP эмодзи стоят 1 codepoint, но 2 utf-16
    unit'а: бюджет, посчитанный в codepoint'ах, может пропустить сообщение,
    реально превышающее жёсткий лимит 32768.
    """
    emoji = "🔥"  # U+1F525 — вне BMP: 1 codepoint, 2 UTF-16 unit'а
    subtitle = emoji * 30 + " - " + emoji * 60
    rows = [row(f"i{n}", subtitle=subtitle) for n in range(400)]
    out = render(rows, START, END, app_name="Example", project="p",
                 app_id="1:2:android:3", versions=["5.15.0"])[0]
    utf16_len = len(out.encode("utf-16-le")) // 2
    assert utf16_len <= 32768, (
        f"вышли за жёсткий лимит Telegram: {utf16_len} utf-16 code units"
    )


def test_daily_issue_name_is_a_link_to_the_issue():
    out = render([row("abc")], START, END, app_name="Example", project="proj",
                 app_id="1:2:android:3", versions=["5.15.0"])[0]
    assert "[IllegalStateException](https://" in out
    assert "/issues/abc)" in out


def test_daily_class_comes_from_subtitle_not_title():
    """В title лежит обфусцированный фрейм: rsplit по нему даёт мусор вроде 'a'."""
    r = row("x", title="SourceFile - B1.g$a$a.a",
            subtitle="java.lang.IllegalStateException - Apply is called")
    out = render([r], START, END, app_name="Example", project="p",
                 app_id="1:2:android:3", versions=["5.15.0"])[0]
    assert "IllegalStateException" in out
    assert "[a]" not in out


def test_daily_has_no_error_type_column():
    out = render([row("a")], START, END, app_name="Example", project="p",
                 app_id="1:2:android:3", versions=["5.15.0"])[0]
    assert out.splitlines()[4] == "| Ошибка | События | Юзеров |"


def test_daily_empty_names_versions_and_known_count():
    out = render([], START, END, app_name="Example", project="p",
                 app_id="1:2:android:3", scanned=46, versions=["5.15.0"])[0]
    assert "# ✅ Example — новых падений нет" in out
    assert "📦 Версии: **5\\.15\\.0**" in out
    assert "📊 Известных issue: **46**" in out


def test_daily_returns_exactly_one_message():
    """Нарезки больше нет: частично доставленная серия оставляла состояние
    несдвинутым и повторяла уже показанные issue на следующем прогоне.
    """
    rows = [row(f"i{n}") for n in range(45)]
    assert len(render(rows, START, END, app_name="Example", project="p",
                      app_id="1:2:android:3", versions=["5.15.0"])) == 1


def test_daily_message_escaping_with_special_chars():
    """Escaping of message text prevents table corruption. A bare | in the message
    would add columns if unescaped; other Markdown specials could break rendering.
    """
    out = render([row("a", subtitle="java.lang.Boom - Error | with * asterisk")], START, END,
                 app_name="Example", project="p", app_id="1:2:android:3",
                 versions=["5.15.0"])[0]
    # Pipes and asterisks in message must be escaped
    # Look for escaped forms: \| and \*
    assert "\\|" in out, "Pipes in message should be escaped"
    assert "\\*" in out, "Asterisks in message should be escaped"
    # The data row should still parse correctly as a 3-column row
    # Verify by checking the table has the expected structure
    assert out.count("| Ошибка | События | Юзеров |") == 1, "Table header should be intact"


def test_daily_no_separator_in_subtitle_omits_message_line():
    """Subtitle without ' - ' separator produces class name only, no dangling <br>."""
    out = render([row("a", subtitle="android.os.DeadSystemException")], START, END,
                 app_name="Example", project="p", app_id="1:2:android:3",
                 versions=["5.15.0"])[0]
    # Should have the link
    assert "[DeadSystemException](" in out
    # Check that we don't have a dangling <br> in the data row
    lines = out.split('\n')
    data_rows = [l for l in lines if l.startswith('| [')]
    assert len(data_rows) == 1
    # The first cell (class name) should NOT contain <br> when there's no separator
    # If the guard was removed, it would have "<br>" followed by empty message
    assert '<br>' not in data_rows[0], "Class name cell should not contain <br> when no separator in subtitle"


def test_daily_truncates_class_name_and_message_at_bounds():
    """Class names and messages are truncated at documented limits: 30 and 48 chars."""
    long_class = "java.lang.RemoteServiceException$CannotDeliverBroadcastException"
    long_message = "x" * 60
    out = render([row("a", subtitle=f"{long_class} - {long_message}")], START, END,
                 app_name="Example", project="p", app_id="1:2:android:3",
                 versions=["5.15.0"])[0]
    # Class name should be truncated: last component is
    # "RemoteServiceException$CannotDeliverBroadcastException" (~49 chars)
    # After truncation to 30, should be "RemoteServiceException$Canno…"
    assert "RemoteServiceException$Canno" in out, "Class name should be truncated to 30 chars"
    # Message should be truncated to 48 chars plus ellipsis: "xxx...…" where xxx is 47 x's
    # Check that we have the ellipsis after truncation
    assert "…" in out, "Message should be truncated with ellipsis"
    # Ensure we don't have the full 60 x's: if there are 50+ consecutive x's, truncation failed
    import re
    long_runs = re.findall(r'x{50,}', out)
    assert len(long_runs) == 0, "Message should be truncated to 48 chars, not exceed 50 x's"


def test_daily_period_prints_both_ends_in_same_timezone():
    """Period normalization: start and end both printed in end's timezone, not mixed."""
    msk = ZoneInfo("Europe/Moscow")
    start_utc = datetime(2026, 8, 25, 7, 0, tzinfo=timezone.utc)
    end_msk = datetime(2026, 8, 26, 10, 0, tzinfo=msk)  # = 26.08 07:00 UTC

    real_span_hours = (end_msk - start_utc).total_seconds() / 3600
    assert real_span_hours == 24  # Actual span is 24 hours

    [text] = render([row()], start_utc, end_msk, app_name="Example", project="p",
                    app_id="1:2:android:3")

    # start should be printed in MSK (UTC+3), so 07:00 UTC → 10:00 MSK
    assert "25.08 10:00" in text
    assert "26.08 10:00" in text
    # Critical check: NOT "07:00" on the second line (that would show mixed zones)
    # The period line should show "25.08 10:00 → 26.08 10:00" (24-hour span in same zone)
    period_lines = [l for l in text.split('\n') if '→' in l and ('25.08' in l or '26.08' in l)]
    assert len(period_lines) > 0, "Should have a period line with arrow"
    # Verify it doesn't have the old broken format "07:00 → 10:00" (implied 27 hours)
    for line in period_lines:
        if '25.08 07:00' in line or ('26.08' in line and '07:00' in line):
            assert False, "Mixed timezones detected in period line"


def test_daily_does_not_print_last_seen_version():
    """The spec forbids printing lastSeenVersion on the issue line because it contradicts
    the version filter named in the header.
    """
    out = render([row(last_seen_version="5.11.5")], START, END,
                 app_name="Example", project="p", app_id="1:2:android:3",
                 versions=["5.15.0", "5.14.0"])[0]
    # lastSeenVersion should not appear anywhere in the output
    assert "5.11.5" not in out, "lastSeenVersion should not be printed"
    # The versions in header should be the filter versions, not issue's last_seen_version
    assert "5\\.15\\.0" in out or "5.15.0" in out, "Filter versions should be in header"


def test_render_bootstrap_mentions_count():
    [text] = render_bootstrap(412, "Example")
    assert "412" in text


def test_render_error_is_single_message():
    parts = render_error("Crashlytics недоступен", "HTTP 403")
    assert len(parts) == 1
    assert "403" in parts[0]


def test_bootstrap_names_versions_and_count():
    out = render_bootstrap(158, "Example", versions=["5.15.0", "5.14.0"])[0]
    assert "# 🧭 Example — инициализация" in out
    assert "Запомнено **158** issue" in out
    assert "📦 Версии: **5\\.15\\.0, 5\\.14\\.0**" in out
    assert "🗓 Период" not in out, "у инициализации нет окна наблюдения"


def test_error_message_stays_html():
    """Единственная форма, которая уходит старым методом, — формат не меняется."""
    out = render_error("Отказ", "detail <b>")[0]
    assert out.startswith("⚠️ <b>Отказ</b>")
    assert "&lt;b&gt;" in out
    assert "#" not in out.splitlines()[0]


def test_error_message_says_state_was_not_moved():
    out = render_error("Отказ", "detail")[0]
    assert "Состояние не сдвинуто" in out


from crashdigest.digest import (
    Growth,
    WeekTotals,
    render_weekly,
    week_totals,
    weekly_growth,
)

PREV_WEEK = (START - timedelta(days=7), START)
CUR_WEEK = (START, END + timedelta(days=6))


def growth_row(issue_id, *, was, now, users=5, first="5.12.0"):
    from crashdigest.digest import Growth
    return Growth(row=row(issue_id, events=now, users=users, first_seen_version=first),
                  was_events=was)


def weekly(growth, *, top=5, prev_events=50, cur_events=100):
    from crashdigest.digest import WeekTotals, render_weekly
    return render_weekly(
        growth, app_name="Example", project="p", app_id="1:2:android:3",
        versions=["5.15.0"], previous=(START, END), current=(START, END),
        prev_totals=WeekTotals(events=prev_events, users=10),
        cur_totals=WeekTotals(events=cur_events, users=20), top=top,
    )[0]


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


def test_weekly_header_has_both_periods_and_two_labelled_lines():
    out = weekly([growth_row("a", was=10, now=20)])
    assert "# 📈 Фон за неделю — Example" in out
    assert "📦 Версии: " in out
    assert "🗓 Период: " in out


def test_weekly_totals_are_a_table():
    out = weekly([growth_row("a", was=10, now=20)])
    assert "|  | было | стало | Δ |" in out
    assert "| события |" in out
    assert "| юзеров |" in out


def test_weekly_growth_table_has_six_columns():
    out = weekly([growth_row("a", was=10, now=20)])
    assert "| Ошибка | Известен с | Было | Стало | Δ | Юзеров |" in out


def test_weekly_issue_name_is_a_link():
    out = weekly([growth_row("abc", was=10, now=20)])
    assert "/issues/abc)" in out


def test_weekly_tail_goes_into_a_details_block():
    out = weekly([growth_row(f"i{n}", was=1, now=100 - n) for n in range(12)], top=5)
    assert "<details><summary>📂 Ещё 7 растущих</summary>" in out
    assert "</details>" in out


def test_weekly_without_a_tail_has_no_details_block():
    out = weekly([growth_row(f"i{n}", was=1, now=10) for n in range(3)], top=5)
    assert "<details" not in out


def test_weekly_says_so_when_nothing_grows():
    out = weekly([])
    assert "Растущих нет — фон не увеличивается." in out
    assert "<details" not in out


def test_weekly_arrow_follows_the_direction_of_events():
    up = weekly([growth_row("a", was=1, now=9)], prev_events=10, cur_events=50)
    down = weekly([growth_row("a", was=1, now=9)], prev_events=50, cur_events=10)
    assert up.startswith("# 📈")
    assert down.startswith("# 📉")


def test_weekly_totals_values_in_correct_columns():
    """Totals table column order must be: было | стало | Δ. Values must not be swapped."""
    # Use asymmetric values: было=30, стало=80, Δ=+50
    out = weekly([], prev_events=30, cur_events=80)
    # Check both rows have correct values in correct columns
    assert '| события | 30 | **80** | +50 |' in out, \
        "Events row has wrong values or order"
    assert '| юзеров | 10 | **20** | +10 |' in out, \
        "Users row has wrong values or order"


def test_weekly_growth_table_values_in_correct_columns():
    """Growth table columns: Было | Стало | Δ. Values must not be swapped."""
    # Use asymmetric values: was_events=25, now=75, delta=+50, users=8
    out = weekly([growth_row("issue_a", was=25, now=75, users=8)])
    # Row should have: | ... | 25 | **75** | **+50** | 8 |
    # Look for the pattern in the table
    assert '25 | **75** | **+50** | 8 |' in out, \
        "Growth values in wrong order or wrong column"


def test_weekly_top_limits_main_growth_table():
    """The top parameter must limit rows in the main growth table, not just the header count."""
    # Create 12 issues, set top=5
    growth = [growth_row(f"i{n}", was=1, now=100 - n) for n in range(12)]
    out = weekly(growth, top=5)
    # Count rows with issue links in the main table (before <details>)
    # Main table should have only 5 rows with links
    lines = out.split('\n')
    main_table_links = 0
    in_details = False
    for line in lines:
        if '<details>' in line:
            in_details = True
        if not in_details and '/issues/i' in line:
            main_table_links += 1
    assert main_table_links == 5, \
        f"Main growth table should have 5 rows (top=5), but found {main_table_links}"
    # Verify header says "5 из 12"
    assert "5 из 12" in out, "Header should say '5 из 12'"


def test_weekly_versions_list_complete():
    """All versions must appear in the header, not just the first one."""
    from crashdigest.digest import WeekTotals, render_weekly
    result = render_weekly(
        [growth_row("a", was=1, now=2)],
        app_name="Example",
        project="p",
        app_id="1:2:android:3",
        versions=["5.15.0", "5.14.0", "5.13.1"],  # Multiple versions
        previous=(START, END),
        current=(START, END),
        prev_totals=WeekTotals(events=50, users=10),
        cur_totals=WeekTotals(events=100, users=20),
        top=5,
    )[0]
    # All three versions must be in the output (with escaped dots)
    assert "5\\.15\\.0, 5\\.14\\.0, 5\\.13\\.1" in result, \
        "All versions should be in header"


def test_pack_and_the_old_limits_are_gone():
    """Нарезка на несколько сообщений больше не нужна и не должна остаться."""
    import crashdigest.digest as d
    assert not hasattr(d, "_pack")
    assert not hasattr(d, "TELEGRAM_LIMIT")
    assert not hasattr(d, "MAX_LISTED")
    assert not hasattr(d, "_fmt_change"), "остался мёртвым после переписывания"
