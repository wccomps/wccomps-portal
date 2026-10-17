from django import template

from core.utils import shorten as shorten_text

register = template.Library()


@register.filter
def shorten(value: object, limit: int) -> str:
    """`{{ text|shorten:50 }}`: a list's preview of long text (core.utils.shorten), in place of truncatechars."""
    return shorten_text("" if value is None else str(value), int(limit))
