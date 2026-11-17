"""Ticketing utilities: atomic ticket creation and clearing.

Status, assignee, and category changes live in ticketing.lifecycle. Async callers use variants with an 'a'
prefix (e.g. aclear_all_tickets) made with _make_async().
"""

import functools
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from core.models import QueuedTask
from core.queued_tasks import TicketCreated
from core.utils import split_message
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory, TicketHistory

# What create_ticket_atomic numbers a ticket: T, the team's number and the team's ticket count, each at least 3 digits
TICKET_NUMBER_RE = re.compile(r"T\d{3,}-\d{3,}")

# The most messages a ticket's comment or update takes in a chat; the ticket's page shows the rest
RELAY_MAX_PARTS = 2


def storable(text: str) -> str:
    """A chat message's text as a comment stores it: without NUL characters, which PostgreSQL text can't hold. The
    message is already in chat, so it is kept rather than refused."""
    return text.replace("\x00", "")


def ticket_url(ticket_number: str) -> str:
    """The ticket's page, as a link in chat (BASE_URL)."""
    return settings.BASE_URL.rstrip("/") + reverse("ticket_detail", args=[ticket_number])


def relay_parts(text: str, limit: int, full_text_at: str) -> list[str]:
    """text as a chat relays it: in messages of at most limit characters (core.utils.split_message), at most
    RELAY_MAX_PARTS of them. Text that takes more is cut short in the last, which ends with full_text_at, the page that
    shows all of it, so one long text can't hold up the queue with message after message."""
    parts = split_message(text, limit)
    if len(parts) <= RELAY_MAX_PARTS:
        return parts
    pointer = f"\n[...] The rest is on the portal: {full_text_at}"
    return [*parts[: RELAY_MAX_PARTS - 1], parts[RELAY_MAX_PARTS - 1][: limit - len(pointer)] + pointer]


def _make_async[**P, T](fn: Callable[P, T]) -> Callable[P, Awaitable[T]]:

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await sync_to_async(fn)(*args, **kwargs)

    wrapper.__name__ = f"a{fn.__name__}"
    wrapper.__qualname__ = f"a{fn.__qualname__}"
    return wrapper


def get_user_for_ticket(
    discord_id: int | None = None,
    user: User | None = None,
) -> User | None:
    """Return `user` if given, else the User behind an active DiscordLink for `discord_id`."""
    if user:
        return user

    if discord_id:
        discord_link = DiscordLink.objects.filter(discord_id=discord_id, is_active=True).first()
        if discord_link and discord_link.user:
            return discord_link.user

    return None


# Every ticket posts to Discord (thread, dashboard, queue), and cancelling posts again, so a team
# creating and cancelling in a loop can flood the server. Cancelled tickets still count.
TEAM_TICKET_LIMIT = 8
TEAM_TICKET_WINDOW = timedelta(minutes=10)


class TicketRateLimitError(Exception):
    """The team has created too many tickets recently."""

    def __init__(self) -> None:
        minutes = int(TEAM_TICKET_WINDOW.total_seconds() // 60)
        super().__init__(
            f"Your team has created too many tickets in the last {minutes} minutes. "
            "Please wait a few minutes, or add details to an existing ticket instead."
        )


def create_ticket_atomic(
    team: Team,
    category: TicketCategory,
    title: str,
    description: str = "",
    hostname: str = "",
    ip_address: str | None = None,
    service_name: str = "",
    actor_username: str = "system",
    enforce_team_limit: bool = True,
) -> Ticket:
    """Create a ticket with an atomically generated ticket number, and queue the bot task that gives it its
    Discord thread in the same transaction, so a ticket never exists without that task.

    Raises TicketRateLimitError when enforce_team_limit is set (False for staff filing on a
    team's behalf) and the team hit TEAM_TICKET_LIMIT.
    """
    with transaction.atomic():
        # Lock the team row to prevent concurrent ticket creation
        team = Team.objects.select_for_update().get(pk=team.pk)

        # Counted under the team lock, so concurrent requests can't all slip under the limit
        if enforce_team_limit:
            since = timezone.now() - TEAM_TICKET_WINDOW
            if Ticket.objects.filter(team=team, created_at__gte=since).count() >= TEAM_TICKET_LIMIT:
                raise TicketRateLimitError

        team.ticket_counter = F("ticket_counter") + 1
        team.save(update_fields=["ticket_counter"])
        team.refresh_from_db()

        sequence = team.ticket_counter
        ticket_number = f"T{team.team_number:03d}-{sequence:03d}"

        ticket = Ticket.objects.create(
            ticket_number=ticket_number,
            team=team,
            category=category,
            title=title,
            description=description,
            hostname=hostname,
            ip_address=ip_address or None,
            service_name=service_name,
            status=Ticket.STATUS_OPEN,
            points_charged=category.points,
        )

        TicketHistory.objects.create(
            ticket=ticket,
            action="created",
            details={"created_by": actor_username},
        )
        QueuedTask.enqueue(TicketCreated(ticket_id=ticket.id))

    return ticket


def clear_all_tickets(actor: str) -> dict[str, int]:
    """Delete every ticket (comments, attachments and history cascade) and reset team counters.

    Audited as clear_tickets; returns what was removed.
    """
    from core.models import AuditLog
    from ticketing.models import TicketAttachment, TicketComment

    with transaction.atomic():
        Team.objects.all().lock()
        counts = {
            "tickets_deleted": Ticket.objects.count(),
            "attachments_deleted": TicketAttachment.objects.count(),
            "comments_deleted": TicketComment.objects.count(),
            "history_deleted": TicketHistory.objects.count(),
            "teams_reset": Team.objects.filter(ticket_counter__gt=0).count(),
        }
        Ticket.objects.all().delete()
        Team.objects.filter(ticket_counter__gt=0).update(ticket_counter=0)
        AuditLog.objects.create(
            action="clear_tickets", admin_user=actor, target_entity="tickets", target_id=0, details=counts
        )
    return counts


aclear_all_tickets = _make_async(clear_all_tickets)
