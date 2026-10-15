"""The component variant each stored status is shown with.

A badge or alert given a value it doesn't know falls back to its default look without an error, so a status
shown straight from the database (variant="{{ dist.email_status }}") rendered "delivered" and "bounced" as
plain grey badges. Templates map a status here instead (|badge_variant:"packet_email"), and
test_template_vocabulary checks every stored value is mapped to a variant the component knows.
"""

# kind -> (model label, field, {stored value: c-badge variant})
BADGE_STATUSES: dict[str, tuple[str, str, dict[str, str]]] = {
    "ticket": (
        "ticketing.Ticket",
        "status",
        {"open": "info", "claimed": "warning", "resolved": "success", "cancelled": "neutral"},
    ),
    "packet": (
        "packets.Packet",
        "status",
        {"draft": "warning", "distributing": "warning", "completed": "success", "cancelled": "neutral"},
    ),
    "registration": (
        "registration.TeamRegistration",
        "status",
        {
            "pending": "warning",
            "approved": "info",
            "paid": "success",
            "credentials_sent": "neutral",
            "rejected": "danger",
        },
    ),
    "packet_email": (
        "packets.PacketDistribution",
        "email_status",
        {"pending": "neutral", "sent": "success", "delivered": "success", "failed": "danger", "bounced": "danger"},
    ),
}

# Django message level tag -> c-alert variant
MESSAGE_ALERTS: dict[str, str] = {
    "debug": "info",
    "info": "info",
    "success": "success",
    "warning": "warning",
    "error": "error",
}


def badge_variant(value: str, kind: str) -> str:
    return BADGE_STATUSES[kind][2].get(value, "default")


def alert_variant(tag: str) -> str:
    return MESSAGE_ALERTS.get(tag, "info")
