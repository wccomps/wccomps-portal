"""What a screen reader hears from the components that change in front of it: a sorted column and a running
operation's progress."""

import re

import pytest
from django.template import RequestContext, Template
from django.test import RequestFactory
from django_cotton.compiler_regex import CottonCompiler


def render(source: str, **context: object) -> str:
    return Template(CottonCompiler().process("{% load cotton %}" + source)).render(
        RequestContext(RequestFactory().get("/teams/"), context)
    )


def header(sort: str) -> str:
    html = render('<c-table_header sort_field="team" current_sort="{{ sort }}">Team</c-table_header>', sort=sort)
    match = re.search(r"<th\b[^>]*>", html)
    assert match
    return match.group(0)


@pytest.mark.parametrize(("sort", "state"), [("team", "ascending"), ("-team", "descending")])
def test_a_sorted_column_says_which_way(sort: str, state: str) -> None:
    assert f'aria-sort="{state}"' in header(sort)


@pytest.mark.parametrize("sort", ["", "default", "name", "-name"])
def test_a_column_not_sorted_on_says_nothing(sort: str) -> None:
    assert "aria-sort" not in header(sort)


def test_the_sort_arrow_is_drawn_not_read() -> None:
    """The arrow comes from the stylesheet, keyed on aria-sort: in the text it would be read as a triangle."""
    html = render('<c-table_header sort_field="team" current_sort="{{ sort }}">Team</c-table_header>', sort="team")
    assert "▲" not in html
    assert "▼" not in html


def test_progress_steps_are_announced_and_the_bar_is_named() -> None:
    html = render("<c-progress_bar />")
    assert re.search(r'<span[^>]*role="status"[^>]*x-text="progressStep"', html)
    assert re.search(r'role="progressbar"[^>]*aria-label="', html)
