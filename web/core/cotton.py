"""Escaping for the attributes a django-cotton component writes with {{ attrs }}.

django-cotton (tested on 2.7.2; 2.8.0 has the same code) joins {{ attrs }} itself and marks the result safe without
escaping it, so a value passed with :name="value" or name=value is written unescaped, and one that starts and ends
with `"` closes its attribute. escape_component_attrs makes every value one double-quoted attribute. A safe string
only has `"` encoded: a value with template tags (the engine escaped it), a quoted :name="'text'" literal (template
source, which Django marks safe like {{ "text" }}), or a value a view marked safe. Any other value is escaped, a
plain literal (name="text") included, so an entity belongs only in a value with template tags (test_template_lint).

It also stops cotton silently dropping a :name the page's context doesn't have (usually an Alpine binding meant as
::name): under settings.STRICT_COMPONENT_ATTRS (DEBUG and the tests) the render fails; in production it is reported
to wccomps.errors once per place and the component renders as cotton would without it.
"""

import inspect
import logging
import re

from django.conf import settings
from django.template import TemplateSyntaxError
from django.utils.html import escape
from django.utils.safestring import SafeData, SafeString, mark_safe
from django_cotton.templatetags import Attrs

# Characters that can't end an attribute name or start markup; a :attrs spread's keys come from data
ATTRIBUTE_NAME = re.compile(r"[^\s\"'<>/=\x00-\x1f\x7f]+")

error_logger = logging.getLogger("wccomps.errors")
cotton_unprocessable = Attrs.unprocessable
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


def tag_location() -> str:
    """Where the component tag is: cotton's component node calls unprocessable from its render."""
    frame = inspect.currentframe()
    node = frame.f_back.f_back.f_locals.get("self") if frame and frame.f_back and frame.f_back.f_back else None
    origin, token = getattr(node, "origin", None), getattr(node, "token", None)
    return f"{origin.name}:{token.lineno}" if origin and token else "a template"


def unresolved(attrs: Attrs, name: str) -> None:
    """Called by cotton for each :name it can't resolve, on any component, before it leaves the attribute out."""
    where = tag_location()
    problem = (
        f"{where}: :{name} isn't in the page's context, so the component leaves it out; an Alpine binding is ::{name}"
    )
    if settings.STRICT_COMPONENT_ATTRS:
        raise TemplateSyntaxError(problem)
    if f"{where}:{name}" not in reported:
        reported.add(f"{where}:{name}")
        error_logger.error(problem)
    cotton_unprocessable(attrs, name)


def escape_component_attrs() -> None:
    """Install the above into django-cotton. CoreConfig.ready calls it."""
    Attrs.__str__ = attrs_html
    Attrs.unprocessable = unresolved
