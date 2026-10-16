from collections.abc import ItemsView

from django import template

from core.utils import APPROVAL_STATUSES

register = template.Library()


@register.simple_tag
def approval_statuses() -> ItemsView[str, str]:
    """The approval-status filter's values and labels."""
    return APPROVAL_STATUSES.items()
