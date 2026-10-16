"""Escaping for the attributes a django-cotton component writes with {{ attrs }}.

django-cotton (tested on 2.7.2; 2.8.0 has the same code) joins {{ attrs }} itself and marks the result safe without
escaping it, so a value passed with :name="value" or name=value is written unescaped, and one that starts and ends
with `"` closes its attribute. escape_component_attrs makes every value one double-quoted attribute. A safe string
only has `"` encoded: a value with template tags (the engine escaped it), a quoted :name="'text'" literal (template
source, which Django marks safe like {{ "text" }}), or a value a view marked safe. Any other value is escaped, a
plain literal (name="text") included: in one, write the character, not an entity (test_template_lint checks).

It also stops cotton silently dropping a :name the page's context doesn't have (usually an Alpine binding meant as
::name): under settings.STRICT_COMPONENT_ATTRS (DEBUG and the tests) the render fails; in production it is reported
to wccomps.errors once per template, component and name in each worker process, and the component renders as cotton
would without it.
"""

import logging
import re
from contextvars import ContextVar
from typing import cast

from django.conf import settings
from django.template import Context, TemplateSyntaxError
from django.utils.html import escape
from django.utils.safestring import SafeData, SafeString, mark_safe
from django_cotton.templatetags import Attrs
from django_cotton.templatetags._component import CottonComponentNode

# Characters that can't end an attribute name or start markup; a :attrs spread's keys come from data
ATTRIBUTE_NAME = re.compile(r"[^\s\"'<>/=\x00-\x1f\x7f]+")

error_logger = logging.getLogger("wccomps.errors")
cotton_unprocessable = Attrs.unprocessable
cotton_render = CottonComponentNode.render
# The component tag being rendered, so a dropped attribute can say where it was passed
rendering: ContextVar[CottonComponentNode | None] = ContextVar("rendering", default=None)
reported: set[str] = set()


def attribute(name: str, value: object) -> str:
    if not ATTRIBUTE_NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not an attribute name a component can write")
    if value is True:
        return name
    if isinstance(value, SafeData):
        return f'{name}="{str(value).replace('"', "&quot;")}"'
    return f'{name}="{escape(value)}"'


def attrs_html(attrs: Attrs) -> SafeString:
    return mark_safe(" ".join(attribute(name, value) for name, value in attrs.attrs_dict().items()))  # noqa: S308


def render_tracked(node: CottonComponentNode, context: Context) -> str:
    token = rendering.set(node)
    try:
        return cast(str, cotton_render(node, context))  # SafeString, kept as it is
    finally:
        rendering.reset(token)


def tag_location() -> str:
    """The template and component of the tag being rendered: <template name>: <c-component>."""
    node = rendering.get()
    origin = getattr(node, "origin", None)
    template = (origin.template_name or origin.name) if origin else "a template"
    return f"{template}: <c-{getattr(node, 'component_name', '?')}>"


def unresolved(attrs: Attrs, name: str) -> None:
    """Called by cotton for each :name it can't resolve, on any component, before it leaves the attribute out."""
    where = tag_location()
    problem = f"{where} :{name} isn't in the page's context, so the component leaves it out (Alpine: ::{name})"
    if settings.STRICT_COMPONENT_ATTRS:
        raise TemplateSyntaxError(problem)
    if f"{where} {name}" not in reported:
        reported.add(f"{where} {name}")
        error_logger.error(problem)
    cotton_unprocessable(attrs, name)


def escape_component_attrs() -> None:
    """Install the above into django-cotton. CoreConfig.ready calls it."""
    Attrs.__str__ = attrs_html
    Attrs.unprocessable = unresolved
    CottonComponentNode.render = render_tracked
