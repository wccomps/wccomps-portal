"""Ticketing utilities: atomic ticket creation, category changes and clearing.

Status and assignee changes live in ticketing.lifecycle. Each *_atomic function has an async
variant with an 'a' prefix (e.g. acreate_ticket_atomic) created via _make_async().
"""

import functools
from collections.abc import Awaitable, Callable
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.tickets_config import get_category_config
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory, TicketHistory


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
    """Create a ticket with an atomically generated ticket number.

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

    return ticket


acreate_ticket_atomic = _make_async(create_ticket_atomic)


def change_ticket_category_atomic(
    ticket_id: int,
    new_category_id: int,
    actor_username: str,
    user: User | None = None,
    discord_id: int | None = None,
) -> tuple[Ticket | None, str | None]:
    """Move a ticket to another category atomically, recording both categories' names and points. Only its assignee
    or a ticketing admin (`user` or `discord_id`) does (ticketing.lifecycle.assignee_refusal)."""
    from ticketing.lifecycle import assignee_refusal

    with transaction.atomic():
        ticket = (
            Ticket.objects.select_for_update(of=("self",))
            .select_related("team", "assigned_to")
            .filter(id=ticket_id)
            .first()
        )

        if not ticket:
            return None, "Ticket not found."
        user = get_user_for_ticket(discord_id=discord_id, user=user)
        if error := assignee_refusal(ticket, user, "recategorize"):
            return None, error

        new_cat_info = get_category_config(new_category_id)
        if new_cat_info is None:
            return None, "Invalid category."

        old_category_id = ticket.category_id
        if old_category_id == new_category_id:
            return None, f"Ticket {ticket.ticket_number} is already in that category."
        old_cat_info = get_category_config(old_category_id) or {}

        ticket.category_id = new_category_id
        ticket.save(update_fields=["category", "updated_at"])

        TicketHistory.objects.create(
            ticket=ticket,
            action="category_changed",
            actor=user,
            details={
                "changed_by": actor_username,
                "old_category": old_category_id,
                "old_category_name": old_cat_info.get("display_name", "Unknown"),
                "new_category": new_category_id,
                "new_category_name": new_cat_info.get("display_name", "Unknown"),
                "old_points": old_cat_info.get("points", 0),
                "new_points": new_cat_info.get("points", 0),
            },
        )

        return ticket, None


achange_ticket_category_atomic = _make_async(change_ticket_category_atomic)


def clear_all_tickets(actor: str) -> dict[str, int]:
    """Delete every ticket (comments, attachments and history cascade) and reset team counters.

    Audited as clear_tickets; returns what was removed.
    """
    from core.models import AuditLog
    from ticketing.models import TicketAttachment, TicketComment

    with transaction.atomic():
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
