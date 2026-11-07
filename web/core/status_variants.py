"""The component variant each stored status is shown with, and the class a score is shown in.

A badge or alert given a value it doesn't know falls back to its default look without an error, so a status
shown straight from the database (variant="{{ dist.email_status }}") rendered "delivered" and "bounced" as
plain grey badges. Templates map a status here instead (|badge_variant:"packet_email"), and
test_template_vocabulary checks every stored value is mapped to a variant the component knows.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils.formats import sanitize_separators

logger = logging.getLogger(__name__)

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
            "rejected": "danger",
        },
    ),
    "packet_email": (
        "packets.PacketDistribution",
        "email_status",
        {"pending": "neutral", "sent": "success", "failed": "danger"},
    ),
    "orange_assignment": (
        "orange_team.OrangeAssignment",
        "status",
        {
            "pending": "neutral",
            "in_progress": "info",
            "submitted": "warning",
            "approved": "success",
            "rejected": "danger",
        },
    ),
    "orange_check_window": (
        "orange_team.OrangeCheck",
        "window_state",
        {
            "open": "success",
            "upcoming": "info",
            "closed": "neutral",
            "unscheduled": "warning",
        },
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

# A score's sign -> the class it is shown in (c-score_value, the admin's net score column)
SCORE_CLASSES: dict[int, str] = {1: "score-positive", 0: "score-zero", -1: "score-negative"}


def badge_variant(value: str, kind: str) -> str:
    """The variant for a stored value. An unknown kind is a template mistake: it raises under DEBUG and is logged
    in production, where the page still renders with the default badge (test_template_vocabulary catches it
    before then)."""
    if kind not in BADGE_STATUSES:
        if settings.DEBUG:
            raise ValueError(f"No badge status kind {kind!r}; kinds: {', '.join(sorted(BADGE_STATUSES))}")
        logger.error("No badge status kind %r", kind)
        return "default"
    return BADGE_STATUSES[kind][2].get(value, "default")


def alert_variant(tag: str) -> str:
    return MESSAGE_ALERTS.get(tag, "info")


def score_class(shown: object) -> str:
    """The class for a score by the sign of the text the page shows for it, so the color and the number agree (0.6
    shown as 1 is positive), read as Django wrote it (thousand separators too). Anything that isn't a finite number
    takes the zero class."""
    try:
        number = Decimal(sanitize_separators(str(shown)))
    except InvalidOperation:
        return SCORE_CLASSES[0]
    if not number.is_finite():
        return SCORE_CLASSES[0]
    return SCORE_CLASSES[(number > 0) - (number < 0)]
