"""A declared component prop the tag doesn't pass stays empty."""

from django.template import Context, Template
from django_cotton.compiler_regex import CottonCompiler


def test_an_unpassed_prop_ignores_a_page_variable_of_its_name() -> None:
    html = Template(CottonCompiler().process("{% load cotton %}<c-module>x</c-module>")).render(
        Context({"title": "From the view"})
    )
    assert "From the view" not in html
