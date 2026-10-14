import json
import re
from pathlib import Path

import pytest

from crashdigest.analysis import (
    SYSTEM_PROMPT,
    AnalysisError,
    _cell,
    LIMIT_CAUSE,
    LIMIT_CHECK,
    LIMIT_WHAT,
    MAX_CHECKS,
    MAX_FRAMES,
    TABLE_ROWS,
    Verdict,
    build_prompt,
    headline,
    parse_answer,
    render_comment,
)
from crashdigest.crashlytics import (
    CrashEvent,
    EventException,
    EventFrame,
    EventThread,
    IssueRow,
    parse_event,
)

FIXTURES = Path(__file__).parent / "fixtures"

ROW = IssueRow(
    issue_id="iss-1", title="SourceFile - B1.g$a$a.a",
    subtitle="java.lang.IllegalStateException - FragmentManager is already executing transactions",
    error_type="FATAL", state="OPEN", last_seen_version="5.15.0",
    first_seen_version="5.14.0", events_count=29, impacted_users=13,
)


def event(context=("breadcrumbs", "custom_keys", "user")):
    """Контекст по умолчанию — дефолтный из config: отбор блоков делает
    `parse_event`, потому что readMask живой API не поддерживает."""
    return parse_event(json.loads((FIXTURES / "event.json").read_text()),
                       context=context)


def frame(symbol="com.example.Foo.bar", file="Foo.kt", line="1",
          owner="DEVELOPER", blamed=False, library=""):
    return EventFrame(symbol=symbol, file=file, line=line, library=library,
                      owner=owner, blamed=blamed)


def bare_event(**kwargs):
    base = dict(
        event_time="", platform="ANDROID", issue_title="", issue_subtitle="",
        exceptions=(), threads=(), blame_frame=None, app_version="",
        device="", operating_system="", os_version="", memory="",
        process_state="",
        orientation="", breadcrumbs=(), logs=(), custom_keys=(), user="",
    )
    base.update(kwargs)
    return CrashEvent(**base)


# --- заголовок -------------------------------------------------------------

def test_headline_uses_exception_class_without_package():
    assert headline(event(), ROW) == "IllegalStateException"


def test_headline_falls_back_to_subtitle_without_exceptions():
    """У native-крашей `exceptions` пуст, и класс остаётся только в subtitle."""
    assert headline(bare_event(), ROW) == "IllegalStateException"


# --- промпт ----------------------------------------------------------------

def test_prompt_contains_stack_with_owners_and_blame_marker():
    text = build_prompt(event(), ROW)
    assert "ProfileFragment.onResume (ProfileFragment.kt:142) [DEVELOPER]" in text
    assert "<-- виноват" in text
    assert "java.lang.IllegalStateException" in text


def test_prompt_contains_issue_numbers_and_device():
    text = build_prompt(event(), ROW)
    assert "Событий: 29, затронуто пользователей: 13" in text
    assert "Redmi 9" in text
    assert "Android (11)" in text


def test_prompt_includes_selected_context_blocks():
    text = build_prompt(event(), ROW)
    assert "screen_view → select_item → screen_view" in text
    assert "auth_state = logged_in" in text
    assert "u-90210" in text


def test_prompt_omits_context_blocks_filtered_out_at_parse():
    """Блок, не разрешённый в CRASH_CONTEXT, не доезжает даже до промпта —
    его выбросил parse_event."""
    text = build_prompt(event(context=()), ROW)
    assert "Analytics" not in text
    assert "Custom keys" not in text
    assert "u-90210" not in text


def test_prompt_omits_empty_context_blocks():
    """Пустые блоки не печатаются вовсе: заголовок без содержимого — шум,
    который модель может принять за «данные есть, но пустые»."""
    text = build_prompt(bare_event(), ROW)
    assert "Analytics" not in text
    assert "Custom keys" not in text
    assert "Логи" not in text


def test_prompt_keeps_developer_frames_beyond_the_cap():
    """Главный инвариант шапки: код команды нельзя потерять ради
    Looper.loop — именно по фреймам DEVELOPER и ведётся разбор."""
    frames = tuple(
        frame(symbol=f"android.os.Noise{i}.run", owner="PLATFORM")
        for i in range(MAX_FRAMES + 20)
    )
    deep = frame(symbol="com.example.Deep.method", owner="DEVELOPER")
    exception = EventException(type="E", message="m", frames=frames + (deep,),
                              blamed=True, nested=False)
    text = build_prompt(bare_event(exceptions=(exception,)), ROW)

    assert "Deep.method" in text
    assert "фреймов не из кода команды опущено" in text
    assert text.count("[PLATFORM]") == MAX_FRAMES


def test_prompt_reports_full_frame_count_even_when_truncated():
    frames = tuple(frame(symbol=f"a.B{i}.c", owner="PLATFORM")
                   for i in range(MAX_FRAMES + 5))
    exception = EventException(type="E", message="", frames=frames,
                               blamed=True, nested=False)
    assert "Стек (45 фреймов):" in build_prompt(
        bare_event(exceptions=(exception,)), ROW
    )


def test_prompt_uses_threads_only_without_exceptions():
    """Для java-исключения стек уже выведен, и повторять его потоком незачем;
    для native-краша поток — единственный источник стека."""
    thread = EventThread(name="main", signal="SIGSEGV", crashed=True,
                         blamed=True, frames=(frame(symbol="libfoo.so"),))
    text = build_prompt(bare_event(threads=(thread,)), ROW)
    assert "Упавший поток main, сигнал SIGSEGV" in text

    with_exception = bare_event(
        threads=(thread,),
        exceptions=(EventException("E", "m", (frame(),), True, False),),
    )
    assert "Упавший поток" not in build_prompt(with_exception, ROW)


# --- ответ модели ----------------------------------------------------------

def test_parse_answer_collects_fields():
    verdict = parse_answer({
        "what_happened": "что-то", "likely_cause": "почему-то",
        "checks": ["раз", "два"],
    })
    assert verdict == Verdict("что-то", "почему-то", ("раз", "два"))


def test_parse_answer_truncates_long_fields():
    verdict = parse_answer({"what_happened": "я" * 2000,
                            "likely_cause": "п" * 2000,
                            "checks": ["ч" * 2000]})
    assert len(verdict.what_happened) == LIMIT_WHAT
    assert len(verdict.likely_cause) == LIMIT_CAUSE
    assert len(verdict.checks[0]) == LIMIT_CHECK


def test_requested_limits_are_below_accepted_ones():
    """На живых крашах модель переписывает заданный лимит на треть. Если
    просить ровно столько, сколько принимаем, обрыв на полуслове становится
    штатным поведением, а не предохранителем."""
    # Числа из промпта берём разбором, а не подстрокой: в тексте стоят
    # переносы строк, и `"600 символов" in prompt` зеленел бы на любом промпте.
    asked = [int(n) for n in re.findall(r"до (\d+) \\?\s*символов", SYSTEM_PROMPT)]
    assert asked == [300, 600, 200], asked
    assert [LIMIT_WHAT, LIMIT_CAUSE, LIMIT_CHECK] == [450, 900, 280]
    for requested, accepted in zip(asked, (LIMIT_WHAT, LIMIT_CAUSE, LIMIT_CHECK)):
        assert accepted > requested


def test_parse_answer_caps_number_of_checks():
    verdict = parse_answer({"what_happened": "x", "likely_cause": "y",
                            "checks": [str(i) for i in range(20)]})
    assert len(verdict.checks) == MAX_CHECKS


def test_parse_answer_squeezes_whitespace():
    verdict = parse_answer({"what_happened": "две\n\nстроки   подряд",
                            "likely_cause": ""})
    assert verdict.what_happened == "две строки подряд"


def test_parse_answer_tolerates_missing_checks():
    assert parse_answer({"what_happened": "x", "checks": "не список"}).checks == ()


def test_parse_answer_rejects_answer_without_substance():
    with pytest.raises(AnalysisError):
        parse_answer({"checks": ["раз"]})


def test_parse_answer_ignores_non_string_fields():
    with pytest.raises(AnalysisError):
        parse_answer({"what_happened": 42, "likely_cause": None})


# --- рендер ----------------------------------------------------------------

def rendered(verdict=None, ev=None, row=ROW):
    return render_comment(
        verdict or Verdict("что", "почему", ("раз", "два")),
        ev or event(), row, project="example-prod", app_id="app-1",
    )


def test_render_headline_is_only_the_crash_name():
    text = rendered()
    assert text.startswith("# [IllegalStateException](")
    assert "example-prod/crashlytics/app/app-1/issues/iss-1" in text


def test_render_contains_all_sections_as_headings():
    text = rendered()
    for title in ("### 💥 Что произошло", "### 📍 Где падает",
                  "### 🔍 Вероятная причина", "### ✅ Что проверить",
                  "### 📋 Контекст"):
        assert title in text


def test_render_blames_frame_first_in_table():
    """Таблица отвечает на вопрос «куда идти в своём коде», поэтому
    виноватый фрейм обязан стоять первой строкой."""
    table = rendered().split("Где падает")[1]
    first = table.split("|:--|:--|")[1].strip().splitlines()[0]
    assert "ProfileFragment\\.onResume:142" in first
    assert "DEVELOPER" in first


def test_render_table_drops_filename_repeating_class():
    """`ProfileFragment.onResume (ProfileFragment.kt:142)` не влезает в ячейку
    и обрезается на самом нужном. Имя файла повторяет класс — его и убираем."""
    table = rendered().split("Где падает")[1].split("### 📋 Контекст")[0]
    assert "ProfileFragment\\.onResume:142" in table
    assert "ProfileFragment\\.kt" not in table


def test_render_table_is_capped():
    frames = tuple(frame(symbol=f"com.example.C{i}.m", file=f"C{i}.kt")
                   for i in range(TABLE_ROWS + 10))
    ev = bare_event(exceptions=(EventException("E", "m", frames, True, False),))
    rows = rendered(ev=ev).split("|:--|:--|")[1].split("\n\n")[0].strip().splitlines()
    assert len(rows) == TABLE_ROWS


def test_render_escapes_markup_from_the_crash():
    """Главный инвариант рендера: текст краша не должен становиться разметкой.
    Telegram не возвращает ошибку на кривой markdown — он возвращает
    правдоподобно испорченное сообщение."""
    verdict = Verdict("упало в <init> | таблице # и *звёздочках*",
                      "причина с [ссылкой](http://x) и _подчерком_", ())
    text = rendered(verdict=verdict)
    assert "&lt;init\\>" in text
    assert "\\|" in text
    assert "\\#" in text
    assert "\\*звёздочках\\*" in text
    assert "\\[ссылкой\\]" in text


def test_render_breaks_model_newlines_with_br():
    """`md.esc` намеренно схлопывает \\n в пробел, поэтому переводы строк
    обязаны стать <br> ДО экранирования — иначе абзац склеится в строку."""
    text = rendered(verdict=Verdict("первая\nвторая", "", ()))
    assert "первая<br>вторая" in text


def test_render_numbers_checks():
    text = rendered(verdict=Verdict("x", "", ("раз", "два", "три")))
    for index in (1, 2, 3):
        assert f"**{index}\\.**" in text


def test_render_omits_empty_sections():
    text = rendered(verdict=Verdict("только это", "", ()))
    assert "Что произошло" in text
    assert "Вероятная причина" not in text
    assert "Что проверить" not in text


def test_render_survives_event_without_any_frames():
    """Событие без стека — не повод терять разбор: словесная часть осталась."""
    text = rendered(ev=bare_event())
    assert "Где падает" not in text
    assert "Что произошло" in text


def test_render_fits_message_budget():
    verdict = parse_answer({
        "what_happened": "я" * 900, "likely_cause": "п" * 900,
        "checks": ["ч" * 900] * MAX_CHECKS,
    })
    assert len(rendered(verdict=verdict)) < 30000


def test_prompt_explains_owner_labels_including_third_party():
    """THIRD_PARTY пришёл с живого API и в схеме Frame.owner не описан. Если
    промпт о нём не знает, модель примет библиотечный фрейм за код команды."""
    assert "THIRD_PARTY" in SYSTEM_PROMPT
    assert "обфусцирован" in SYSTEM_PROMPT


def test_table_survives_fully_obfuscated_stack():
    """Боевое приложение собрано с R8 и без mapping: фреймов DEVELOPER нет
    ни одного, символы вида `q2.f.R`, файл у всех — `SourceFile`."""
    frames = tuple(
        frame(symbol=s, file="SourceFile", line=line, owner="THIRD_PARTY")
        for s, line in (("q2.d$a.a", "20"), ("r2.j.a", "6"), ("p2.b.j", "86"))
    )
    ev = bare_event(
        exceptions=(EventException("n2.c", "Unable to parse preferences proto.",
                                   frames, True, False),),
        blame_frame=frame(symbol="q2.f.R", file="SourceFile", line="2",
                          owner="THIRD_PARTY", blamed=True),
    )
    text = rendered(ev=ev)
    assert "Где падает" in text
    assert "THIRD\\_PARTY" in text
    assert "q2\\.f\\.R:2" in text        # пакет сохранён, он тут единственное осмысленное
    # `SourceFile` — заглушка Java там, где настоящего имени файла нет. У
    # обфусцированной сборки он стоит у всех фреймов подряд и не стоит ни
    # одного символа ячейки.
    assert "SourceFile" not in text


def test_cell_keeps_package_for_obfuscated_symbols():
    """`h$c.B` не говорит ничего, `preferences.protobuf.h$c.B` называет
    библиотеку. Для необфусцированного кода пакет, наоборот, только мешает."""
    obfuscated = frame(symbol="androidx.datastore.preferences.protobuf.h$c.B",
                       file="SourceFile", line="25", owner="SYSTEM")
    assert _cell(obfuscated) == "preferences.protobuf.h$c.B:25"

    normal = frame(symbol="com.example.app.profile.ProfileFragment.onResume",
                   file="ProfileFragment.kt", line="142")
    assert _cell(normal) == "ProfileFragment.onResume:142"


def test_cell_falls_back_to_library_for_native_frames():
    assert _cell(frame(symbol="", file="", line="", library="libflutter.so")) == \
        "libflutter.so"


def test_headline_keeps_package_when_class_is_one_letter():
    """Обфусцированный класс в заголовке: `z` против `protobuf.z`."""
    ev = bare_event(exceptions=(EventException(
        "androidx.datastore.preferences.protobuf.z", "m", (), True, False),))
    assert headline(ev, ROW) == "protobuf.z"


def test_headline_does_not_pad_a_readable_class():
    ev = bare_event(exceptions=(EventException(
        "java.lang.IllegalStateException", "m", (), True, False),))
    assert headline(ev, ROW) == "IllegalStateException"


def test_table_prefers_library_frames_over_os_frames():
    """В обфусцированной сборке фреймов DEVELOPER нет, и без этого яруса
    таблица заполнялась бы Looper.loop и ZygoteInit.main — они одинаковы у
    всех падений на свете."""
    frames = (
        frame(symbol="android.os.Looper.loop", file="Looper.java", line="223",
              owner="PLATFORM"),
        frame(symbol="com.android.internal.os.ZygoteInit.main",
              file="ZygoteInit.java", line="947", owner="SYSTEM"),
        frame(symbol="q2.d$a.a", file="SourceFile", line="20",
              owner="THIRD_PARTY"),
    )
    ev = bare_event(exceptions=(EventException("E", "m", frames, True, False),))
    table = rendered(ev=ev).split("Где падает")[1].split("### 📋 Контекст")[0]
    first = table.split("|:--|:--|")[1].strip().splitlines()[0]
    assert "q2\\.d$a\\.a:20" in first


def test_context_table_lists_crash_surroundings():
    """Контекст — отдельная секция таблицей: перечислением через запятую он
    читался как хвост предыдущего абзаца, а не как отдельный блок."""
    text = render_comment(Verdict("что", "почему", ()), event(), ROW,
                          project="p", app_id="a",
                          model="gemini-claude-opus-4-6-thinking")
    context = text.split("### 📋 Контекст")[1]
    assert "| 📦 Версия | 5\\.15\\.0 \\(515\\) |" in context
    assert "| 📱 Устройство | Redmi 9 |" in context
    assert "| 🖥 ОС | Android \\(11\\) |" in context
    assert "| 📊 Влияние | 29 событий / 13 юзеров |" in context
    # Модель в обратных кавычках и БЕЗ экранирования — как расписание в
    # стартовом сообщении: внутри code-span слэш показался бы слэшем.
    assert "| 🤖 Модель | `gemini-claude-opus-4-6-thinking` |" in context


def test_context_table_omits_model_row_when_unknown():
    assert "Модель" not in rendered().split("### 📋 Контекст")[1]


def test_context_table_omits_empty_fields():
    """Пустая метка хуже отсутствующей."""
    context = rendered(ev=bare_event()).split("### 📋 Контекст")[1]
    assert "Версия" not in context
    assert "Устройство" not in context
    assert "Влияние" in context          # числа issue есть всегда


def test_os_falls_back_to_platform_when_google_reports_unknown():
    """У боевого приложения displayName ОС приходит как `Unknown (13)`: имя
    не определено, версия определена. Платформа известна точно, и собрать
    `Android 13` из двух достоверных полей честнее, чем печатать Unknown."""
    ev = bare_event(platform="ANDROID", operating_system="Unknown (13)",
                    os_version="13")
    assert "| 🖥 ОС | Android 13 |" in rendered(ev=ev).split("### 📋 Контекст")[1]


def test_os_keeps_a_good_display_name_as_is():
    ev = bare_event(platform="ANDROID", operating_system="Android (11)",
                    os_version="11")
    context = rendered(ev=ev).split("### 📋 Контекст")[1]
    assert "| 🖥 ОС | Android \\(11\\) |" in context


def test_render_keeps_model_paragraphs_apart():
    """600 символов причины одним куском — стена. Пустая строка в ответе
    модели обязана остаться разрывом абзаца, а не схлопнуться в пробел."""
    text = rendered(verdict=Verdict("x", "первый абзац\n\nвторой абзац", ()))
    assert "первый абзац<br><br>второй абзац" in text


def test_render_separates_checks_with_blank_lines():
    """Пункты по 200 символов, склеенные одним <br>, слипаются в абзац."""
    text = rendered(verdict=Verdict("x", "", ("раз", "два")))
    assert "**1\\.** раз<br><br>**2\\.** два" in text


def test_prompt_asks_for_paragraphs_in_the_cause():
    """Разрыв абзаца может появиться только из ответа модели — значит её
    надо об этом просить, иначе `_paragraph` нечего разбивать."""
    assert "абзаца" in SYSTEM_PROMPT
