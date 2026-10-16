"""Escaping for the attributes a django-cotton component writes with {{ attrs }}.

django-cotton (through 2.8.0) joins {{ attrs }} itself and marks the result safe without escaping it, so a value
passed with :name="value" or name=value goes into the page as raw markup, and one that starts and ends with `"`
closes its attribute. escape_component_attrs makes every value an escaped, double-quoted attribute:
- a value from the page's context is escaped, like any {{ value }};
- a value the template engine already escaped (name="{{ value }}", a safe string) is written as it is;
- a literal in the template's source (name="text", ::name="text") is HTML the template author wrote, so an
  entity in it (&quot;, &amp;) stays the character it stands for.
In the last two, only `"` is encoded, so the value can't close its attribute.
"""

import re

from django.utils.html import escape
from django.utils.safestring import SafeData, SafeString, mark_safe
from django_cotton.templatetags import Attrs, _component

# What HTML allows in an attribute name: anything else could end the name and start markup
ATTRIBUTE_NAME = re.compile(r"[^\s\"'<>/=\x00-\x1f\x7f]+")


prepare_attrs = _component._prepare_attrs


class SourceText(str):
    """An attribute value written literally in a template: HTML, but not marked safe, so a component that prints
    it with {{ name }} still escapes it as before."""

    __slots__ = ()


def attribute(name: str, value: object) -> str:
    if not ATTRIBUTE_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not an attribute name a component can write")
    if value is True:
        return name
    if isinstance(value, SafeData | SourceText):
        return f'{name}="{str(value).replace('"', "&quot;")}"'
    return f'{name}="{escape(value)}"'


def attrs_html(attrs: Attrs) -> SafeString:
    return mark_safe(" ".join(attribute(name, value) for name, value in attrs.attrs_dict().items()))  # noqa: S308


def mark_source_literals(attrs: dict[str, object], active_library: object) -> list[object]:
    """django-cotton's _prepare_attrs, with each value it writes as the template spelled it marked SourceText."""
    literal_kinds = {_component.AttrKind.STATIC, _component.AttrKind.UNQUOTED}
    return [
        prepared._replace(value=SourceText(prepared.value))
        if isinstance(prepared.value, str)
        and (
            prepared.kind in literal_kinds
            or (prepared.kind == _component.AttrKind.ESCAPED and prepared.compiled is None)
        )
        else prepared
        for prepared in prepare_attrs(attrs, active_library)
    ]


def escape_component_attrs() -> None:
    """Install the escaping above into django-cotton. CoreConfig.ready calls it once, before any template loads."""
    Attrs.__str__ = attrs_html
    _component._prepare_attrs = mark_source_literals
