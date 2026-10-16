"""Canary for core.cotton: a value a page passes a component comes out as exactly one attribute that decodes back to
it, for each way the portal passes one, rendered through the real template engine."""

import logging
import os
import subprocess
import sys
from html.parser import HTMLParser

import pytest
from django.template import Context, Template, TemplateSyntaxError
from django.test import override_settings
from django.utils.safestring import mark_safe
from django_cotton.compiler_regex import CottonCompiler
from hypothesis import example, given
from hypothesis import strategies as st

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
    :data-safe="safe">Go</c-button>"""


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
            "data-template-literal": "{{ value }}",  # cotton reads a quoted :name as a string literal, unrendered
            "data-safe": '<b class="x">ok</b>',
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
def test_in_production_a_dynamic_attribute_not_in_the_context_is_left_out_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    template = Template(CottonCompiler().process('<c-button :disabled="loading" title="Go">Go</c-button>'))
    with caplog.at_level(logging.WARNING, logger="core.cotton"):
        html = template.render(Context({}))
    assert 'title="Go"' in html
    assert "disabled" not in html
    assert "::disabled" in caplog.text


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


def test_django_setup_installs_the_escaping_before_anything_can_render() -> None:
    """core.cotton patches render-time code (Attrs), so it holds for every template if django.setup(), which every
    entry point (wsgi, manage.py, the bot) runs before rendering, installs it. A fresh interpreter checks that."""
    script = (
        "import django\n"
        "from django_cotton.templatetags import Attrs\n"
        "unpatched = Attrs.__str__\n"
        "django.setup()\n"
        "from core import cotton\n"
        "assert unpatched is not cotton.attrs_html\n"
        "assert Attrs.__str__ is cotton.attrs_html and Attrs.unprocessable is cotton.unresolved\n"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "portal.test_settings"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr
