from django import template

from core import status_variants

register = template.Library()


@register.filter
def badge_variant(value: str, kind: str) -> str:
    """The c-badge variant a stored status is shown with: {{ ticket.status|badge_variant:"ticket" }}."""
    return status_variants.badge_variant(value, kind)


@register.filter
def alert_variant(tag: str) -> str:
    """The c-alert variant a Django message is shown with: {{ message.level_tag|alert_variant }}."""
    return status_variants.alert_variant(tag)
