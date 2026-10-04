import re

from django import template

register = template.Library()

_UNSAFE = re.compile(r"\W")


@register.filter
def jskey(value: object) -> str:
    """A safe JavaScript identifier for `value`, for Alpine bindings keyed by a row id.

    The CSP Alpine build only evaluates simple property paths, so a reactive per-row
    checkbox binds ``:checked="selectedMap.{{ id|jskey }}"``. Prefixing with ``k`` and
    replacing every non-word character keeps the key a valid identifier even when the id
    is a ticket number like ``T001-001`` (which would otherwise break the path).
    """
    return "k" + _UNSAFE.sub("_", str(value))
