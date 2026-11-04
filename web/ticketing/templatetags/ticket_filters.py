from django import template

register = template.Library()


@register.filter
def format_history_action(action: str) -> str:
    """Format a TicketHistory action slug into a readable label."""
    if not action or not isinstance(action, str):
        return ""
    return action.replace("_", " ")


@register.filter
def format_history_details(details: dict[str, object]) -> str:
    """Format ticket history details, showing only meaningful extras."""
    if not details or not isinstance(details, dict):
        return ""

    parts = []

    if "old_category_name" in details and "new_category_name" in details:
        parts.append(f"{details['old_category_name']} to {details['new_category_name']}")

    if "vm_name" in details and "snapshot" in details:
        parts.append(f"{details['vm_name']} @ {details['snapshot']}")

    parts.extend(str(details[key]) for key in ("rollback_upid", "start_upid", "error") if details.get(key))

    if "old_points" in details and "points_charged" in details:
        parts.append(f"{details['old_points']} to {details['points_charged']} pts")
    elif "points_charged" in details:
        parts.append(f"{details['points_charged']} pts")

    if details.get("refunded_points"):
        parts.append(f"{details['refunded_points']} pts refunded")

    if details.get("new_assignee"):
        if details.get("previous_assignee"):
            parts.append(f"{details['previous_assignee']} to {details['new_assignee']}")
        else:
            parts.append(f"to {details['new_assignee']}")

    # Notes and reasons, under whichever key the action that wrote the entry used
    parts.extend(str(details[key]) for key in ("notes", "reason") if details.get(key))

    return " · ".join(parts)
