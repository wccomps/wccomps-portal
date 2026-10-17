from django.template import Context, Template
from hypothesis import example, given
from hypothesis import strategies as st

from core.utils import open_parentheses, shorten


def test_short_text_is_unchanged() -> None:
    assert shorten("Default credentials", 50) == "Default credentials"


def test_a_cut_ends_at_a_word_and_closes_its_parenthesis() -> None:
    text = "Login using weak/guessable passwords (password123, admin/admin, etc)"
    assert shorten(text, 50) == "Login using weak/guessable passwords (password123...)"


def test_a_word_longer_than_the_limit_is_cut_inside_it() -> None:
    assert shorten("a" * 30, 10) == "a" * 10 + "..."


def test_a_break_early_in_the_text_is_not_used() -> None:
    assert shorten("My screenshot_from_competition_day.png", 25) == "My screenshot_from_compet..."


def test_any_whitespace_is_a_break() -> None:
    assert shorten("Line one here\nSecond line", 15) == "Line one here..."


def test_the_filter_cuts_in_a_template() -> None:
    template = Template("{{ text|shorten:12 }}")
    assert template.render(Context({"text": "one two (three four)"})) == "one two..."


@example(text="a (b c (d e f) g h", limit=8)
@example(text=") a (b c d", limit=6)
@given(text=st.text(alphabet=st.sampled_from("ab ()., \n"), max_size=80), limit=st.integers(1, 60))
def test_a_cut_is_a_prefix_that_leaves_no_parenthesis_open(text: str, limit: int) -> None:
    result = shorten(text, limit)
    if len(text) <= limit:
        assert result == text
        return
    body = result.rstrip(")")
    closing = result[len(body) :]
    assert body.endswith("...")
    body = body.removesuffix("...")
    assert text.startswith(body) and len(body) <= limit
    assert closing == ")" * open_parentheses(body)
    assert open_parentheses(result) == 0
