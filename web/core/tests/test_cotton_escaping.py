"""Canary for core.cotton: a value a page passes a component comes out as exactly one attribute that decodes back to
it, for each way the portal passes one, rendered through the real template engine."""

import logging
from html.parser import HTMLParser

import pytest
from django.template import Context, Template, TemplateSyntaxError, Variable
from django.test import override_settings
from django.utils.safestring import mark_safe
from django_cotton.compiler_regex import CottonCompiler
from django_cotton.templatetags import Attrs
from hypothesis import example, given
from hypothesis import strategies as st

from core import cotton

BREAKOUTS = [
    '"><svg onload=alert(1)>"',
    '" autofocus onfocus="alert(1)"',
    "'><svg onload=alert(1)>",
    "<script>alert(1)</script>",
    '{"open": "\'"}',
    "x' y=\"z",
    "&amp; &lt;b&gt;",
    "a & b",
]

COMPONENT = """<c-button
    :title="value"
    data-unquoted=value
    :data-index="value"
    :attrs="spread"
    aria-description="{{ value }}"
    data-url="{{ url }}"
    :data-raw-url="url"
    @click="open = !open && go('a')"
    ::class="{ 'on': open }"
    x-data='{"open": false}'
    data-static="/list?a=1&b=2"
    ::x-text="{{ value }}"
    data-fallback=not_in_context
    :data-literal="'a literal'"
    :data-template-literal="'{{ value }}'"
    :data-safe="safe"
    :data-quoted="quoted">Go</c-button>"""


class Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))


def render(value: str) -> str:
    context = {
        "value": value,
        "url": "/list?a=1&b=2",
        "spread": {"data-spread": value},
        "safe": mark_safe('<b class="x">ok</b>'),  # noqa: S308
        "quoted": f"'{value}'",
    }
    return Template(CottonCompiler().process(COMPONENT)).render(Context(context))


def button_attributes(html: str) -> dict[str, str | None]:
    parser = Elements()
    parser.feed(html)
    parser.close()
    assert [tag for tag, _ in parser.tags] == ["button"], html
    attributes = parser.tags[0][1]
    assert len({name for name, _ in attributes}) == len(attributes), html
    return dict(attributes)


@given(value=st.text())
@pytest.mark.parametrize("breakout", BREAKOUTS)
@example(value="")
def test_every_attrs_value_comes_out_as_the_value_it_was(breakout: str, value: str) -> None:
    for passed in (breakout, value, breakout + value):
        html = render(passed)
        assert button_attributes(html) == {
            "type": "button",
            "title": passed,
            "data-unquoted": passed,
            "data-index": passed,
            "data-spread": passed,
            "aria-description": passed,
            "data-url": "/list?a=1&b=2",
            "data-raw-url": "/list?a=1&b=2",
            "@click": "open = !open && go('a')",
            ":class": "{ 'on': open }",
            "x-data": '{"open": false}',
            "data-static": "/list?a=1&b=2",
            ":x-text": passed,
            "data-fallback": "not_in_context",
            "data-literal": "a literal",
            # A quoted :name is a Django string literal: template source, marked safe and left unrendered
            "data-template-literal": "{{ value }}",
            "data-safe": '<b class="x">ok</b>',
            "data-quoted": f"'{passed}'",
            "class": "button  ",
        }, html


def test_an_attribute_name_that_would_add_markup_fails_to_render() -> None:
    template = Template(CottonCompiler().process('<c-button :attrs="spread">Go</c-button>'))
    with pytest.raises(ValueError, match="attribute name"):
        template.render(Context({"spread": {"x onload=alert(1)": "1"}}))


def test_a_dynamic_attribute_that_is_not_in_the_context_fails_to_render() -> None:
    template = Template(CottonCompiler().process('<c-button :disabled="loading">Go</c-button>'))
    with pytest.raises(TemplateSyntaxError, match="::disabled"):
        template.render(Context({}))


@override_settings(STRICT_COMPONENT_ATTRS=False)
def test_in_production_a_dynamic_attribute_not_in_the_context_is_left_out() -> None:
    template = Template(CottonCompiler().process('<c-button :disabled="loading_left_out" title="Go">Go</c-button>'))
    html = template.render(Context({}))
    assert 'title="Go"' in html
    assert "disabled" not in html


@pytest.mark.parametrize(
    "source",
    [
        '<c-badge :aria_label="missing">New</c-badge>',  # declared in its <c-vars>
        '<c-pagination :page_obj="missing" />',  # prints no {{ attrs }}
    ],
)
def test_a_dynamic_attribute_not_in_the_context_fails_on_any_component(source: str) -> None:
    with pytest.raises(TemplateSyntaxError, match="isn't in the page's context"):
        Template(CottonCompiler().process(source)).render(Context({}))


def test_an_alpine_binding_renders() -> None:
    html = Template(CottonCompiler().process('<c-button ::disabled="loading">Go</c-button>')).render(Context({}))
    assert ':disabled="loading"' in html


def test_the_installed_attrs_methods_are_core_cotton_ones() -> None:
    """CoreConfig.ready installs them; both run at render time, so they cover every template however early it loaded."""
    assert Attrs.__str__ is cotton.attrs_html
    assert Attrs.unprocessable is cotton.unresolved


@pytest.mark.parametrize(
    ("value", "written"),
    [
        ('<b> & "x"', "&lt;b&gt; &amp; &quot;x&quot;"),
        (mark_safe('<b> &amp; "x"'), "<b> &amp; &quot;x&quot;"),  # noqa: S308
        (Variable("'<b> &amp;'").resolve({}), "<b> &amp;"),
    ],
)
def test_a_plain_value_is_escaped_and_a_safe_one_only_has_its_quotes_encoded(value: str, written: str) -> None:
    assert cotton.attribute("title", value) == f'title="{written}"'


@override_settings(STRICT_COMPONENT_ATTRS=False)
def test_in_production_a_dropped_attribute_is_reported_once_per_place(caplog: pytest.LogCaptureFixture) -> None:
    template = Template(CottonCompiler().process('\n<c-button :disabled="loading_once">Go</c-button>'))
    with caplog.at_level(logging.ERROR, logger="wccomps.errors"):
        template.render(Context({}))
        template.render(Context({}))
    reports = [r for r in caplog.records if r.name == "wccomps.errors" and "::disabled" in r.getMessage()]
    assert len(reports) == 1
    assert ":2" in reports[0].getMessage()
