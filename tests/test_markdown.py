import pytest

from crashdigest.markdown import esc, link, table


def test_escapes_every_special_character():
    """Пропущенный символ не даёт ошибки от Telegram — он молча портит разметку."""
    raw = r"a\b`c*d_e{f}g[h]i(j)k#l+m-n.o!p|q>r~s=t"
    out = esc(raw)
    for ch in r"\`*_{}[]()#+-.!|>~=":
        assert f"\\{ch}" in out, f"символ {ch!r} не экранирован"


def test_newline_becomes_space():
    """Перевод строки внутри ячейки рвёт строку таблицы."""
    assert "\n" not in esc("первая\nвторая")
    assert esc("первая\nвторая") == "первая вторая"


def test_plain_text_survives_unchanged():
    assert esc("IllegalStateException") == "IllegalStateException"


def test_empty_and_none_safe():
    assert esc("") == ""
    assert esc(None) == ""


def test_angle_bracket_in_init_is_not_swallowed():
    """`<init>` — рутинная часть java-стектрейсов, а не патология. Без
    экранирования Telegram разбирает `<init>` как тег и вырезает его молча.
    """
    out = esc("Attempt to invoke <init> on a null object")
    assert "&lt;init" in out, "'<' обязан стать HTML-сущностью, а не голым символом"
    assert "<init>" not in out, "необработанный '<' Telegram распознает как тег"


def test_angle_bracket_does_not_close_our_own_details_tag():
    """Формат сам эмитит `<details>`/`</details>`; `<`, пришедший из API,
    не должен суметь закрыть их раньше времени.
    """
    out = esc("конец </details> тут")
    assert "&lt;/details" in out
    assert "</details>" not in out


def test_angle_bracket_markup_is_not_injected():
    out = esc("<b>жирный</b>")
    assert "<b>" not in out and "</b>" not in out
    assert out.startswith("&lt;b")


def test_literal_html_entity_in_input_is_not_double_decoded():
    """`&` экранируется до `<`: буквальный текст `&lt;` во входе не должен
    превратиться на приёмнике в настоящую угловую скобку.
    """
    out = esc("literal &lt;b&gt; end")
    assert out == "literal &amp;lt;b&amp;gt; end"


def test_link_wraps_text_and_url():
    assert link("класс", "https://x/y") == "[класс](https://x/y)"


def test_table_builds_header_separator_and_rows():
    out = table(("Ошибка", "События"), [["Boom", "12"]], "lr")
    assert out == "| Ошибка | События |\n|:--|--:|\n| Boom | 12 |"


def test_table_alignment_letters_map_to_markdown():
    out = table(("a", "b", "c"), [], "lrl")
    assert out.splitlines()[1] == "|:--|--:|:--|"


def test_table_rejects_align_of_wrong_length():
    """Разъехавшаяся разметка выравнивания ломает таблицу целиком и молча."""
    with pytest.raises(ValueError):
        table(("a", "b"), [], "l")


def test_table_rejects_row_with_wrong_width():
    """Строка с неправильным числом ячеек молча портит разметку таблицы."""
    with pytest.raises(ValueError) as excinfo:
        table(("a", "b"), [["x", "y"], ["short"]], "lr")
    assert "строка 1" in str(excinfo.value)
    assert "1 ячеек" in str(excinfo.value)
    assert "заголовке 2" in str(excinfo.value)


def test_link_escapes_url_special_chars():
    """URL может содержать ) или \\ которые разрывают ссылку если не экранированы."""
    assert link("текст", "https://x.ru/path)with)paren") == "[текст](https://x.ru/path\\)with\\)paren)"
    assert link("текст", "https://x.ru/path\\with\\slash") == "[текст](https://x.ru/path\\\\with\\\\slash)"
