"""Escaping for the attributes a django-cotton component writes with {{ attrs }}.

django-cotton (tested on 2.7.2; 2.8.0 has the same code) joins {{ attrs }} itself and marks the result safe without
escaping it, so a value passed with :name="value" or name=value is written unescaped, and one that starts and ends
with `"` closes its attribute. escape_component_attrs makes every value one double-quoted attribute: a value the
template engine already escaped (a safe string, such as name="{{ value }}") only has `"` encoded, and any other
value, a template literal included, is escaped. So a component attribute in a template is written with the
characters themselves, not entities (test_template_lint checks).

It also stops cotton silently dropping a :name the page's context doesn't have (usually an Alpine binding meant as
::name): under settings.STRICT_COMPONENT_ATTRS (DEBUG and the tests) the render fails, and in production a warning
is logged and the attribute left out, as cotton does.
"""

import logging
import re

from django.conf import settings
from django.template import TemplateSyntaxError
from django.utils.html import escape
from django.utils.safestring import SafeData, SafeString, mark_safe
from django_cotton.templatetags import Attrs

# Characters that can't end an attribute name or start markup; a :attrs spread's keys come from data
ATTRIBUTE_NAME = re.compile(r"[^\s\"'<>/=\x00-\x1f\x7f]+")

logger = logging.getLogger(__name__)


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


def unresolved(attrs: Attrs, name: str) -> None:
    """Called by cotton for each :name it can't resolve, on any component, before it leaves the attribute out."""
    problem = f":{name} isn't in the page's context, so the component leaves it out; an Alpine binding is ::{name}"
    if settings.STRICT_COMPONENT_ATTRS:
        raise TemplateSyntaxError(problem)
    logger.warning(problem)
    attrs._unprocessable.append(name)


def escape_component_attrs() -> None:
    """Install the above into django-cotton. CoreConfig.ready calls it; both run at render time, not parse time."""
    Attrs.__str__ = attrs_html
    Attrs.unprocessable = unresolved
