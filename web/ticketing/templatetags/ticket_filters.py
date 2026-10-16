from django import template

register = template.Library()


@register.filter
def format_history_details(details: dict[str, object]) -> str:
    """Format ticket history details, showing only meaningful extras."""
    if not details or not isinstance(details, dict):
        return ""

    parts = []

    if "old_category_name" in details and "new_category_name" in details:
        parts.append(f"{details['old_category_name']} to {details['new_category_name']}")

    if "points_charged" in details:
        parts.append(f"{details['points_charged']} pts")

    # Notes and reasons, under whichever key the action that wrote the entry used
    parts.extend(
        str(details[key])
        for key in ("notes", "resolution_notes", "approval_notes", "verification_notes", "reason")
        if details.get(key)
    )

    return " · ".join(parts)
