"""Canary for how a component writes {{ attrs }}: every attribute a page passes comes out as exactly one attribute
whose value the browser decodes back to what was passed, whatever the value holds and however it was passed.

django-cotton itself writes attrs unescaped (core.cotton escapes them), so this renders a real component through
the template engine; it fails if an upgrade or a change to core.cotton lets a value out raw.
"""

from html.parser import HTMLParser

import pytest
from django.template import Context, Template
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
    @click="open = !open && go('a', &quot;b&quot;)"
    ::class="{ 'on': open }"
    x-data='{"open": false}'
    data-static="/list?a=1&b=2">Go</c-button>"""


class Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))


def render(value: str) -> str:
    context = {"value": value, "url": "/list?a=1&b=2", "spread": {"data-spread": value}}
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
            "@click": "open = !open && go('a', \"b\")",
            ":class": "{ 'on': open }",
            "x-data": '{"open": false}',
            "data-static": "/list?a=1&b=2",
            "class": "button  ",
        }, html


def test_an_attribute_name_that_would_add_markup_fails_to_render() -> None:
    template = Template(CottonCompiler().process('<c-button :attrs="spread">Go</c-button>'))
    with pytest.raises(ValueError, match="attribute name"):
        template.render(Context({"spread": {"x onload=alert(1)": "1"}}))
