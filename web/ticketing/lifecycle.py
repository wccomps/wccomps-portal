"""Ticket lifecycle: every claim, assignment, unclaim, resolve, cancel and reopen goes through here.

Each function locks the ticket row, checks the move against TRANSITIONS, applies it, writes the
TicketHistory entry and queues the Discord side effects in the same transaction: a
post_ticket_update task (the thread message, which also refreshes the dashboard) and, when someone
is given the ticket, an add_user_to_thread task. Callers add no history or Discord tasks of their own.

Each returns (ticket, None) or (None, error_message) and has an 'a'-prefixed async variant for the bot.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from core.discord_tasks import AddUserToThread, PostTicketUpdate
from core.models import DiscordTask
from core.tickets_config import get_category_config
from team.models import DiscordLink
from ticketing.models import Ticket, TicketHistory
from ticketing.utils import _make_async, get_user_for_ticket

# How long a resolved or cancelled ticket's Discord thread stays open before the bot archives it.
THREAD_ARCHIVE_DELAY = timedelta(seconds=60)


# action -> (statuses it may be applied to, refusal formatted with the current status)
TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "claim": (frozenset({Ticket.STATUS_OPEN}), "This ticket is already {status}."),
    "unclaim": (frozenset({Ticket.STATUS_CLAIMED}), "Cannot unclaim ticket with status: {status}."),
    "reassign": (
        frozenset({Ticket.STATUS_CLAIMED, Ticket.STATUS_RESOLVED, Ticket.STATUS_CANCELLED}),
        "Cannot reassign ticket with status: {status}.",
    ),
    "resolve": (frozenset({Ticket.STATUS_OPEN, Ticket.STATUS_CLAIMED}), "Cannot resolve ticket with status: {status}."),
    "cancel": (frozenset({Ticket.STATUS_OPEN, Ticket.STATUS_CLAIMED}), "Cannot cancel ticket with status: {status}."),
    "reopen": (frozenset({Ticket.STATUS_RESOLVED}), "Cannot reopen - ticket is {status}."),
}


def refusal(ticket: Ticket, action: str) -> str | None:
    """Why `action` can't be applied to the ticket in its current status, or None if it can."""
    sources, message = TRANSITIONS[action]
    return None if ticket.status in sources else message.format(status=ticket.status)


def _lock(ticket_id: int) -> Ticket | None:
    return Ticket.objects.select_for_update(of=("self",)).select_related("team").filter(id=ticket_id).first()


def _record(
    ticket: Ticket,
    action: str,
    actor: User | None,
    actor_username: str,
    details: dict[str, object],
    *,
    announce_as: str | None = None,
    assignee: str = "",
    resolution_notes: str = "",
    points_charged: int = 0,
    reason: str = "",
) -> None:
    """Write the history entry and queue the thread update (posted as `announce_as` if given)."""
    TicketHistory.objects.create(ticket=ticket, action=action, actor=actor, details=details)
    DiscordTask.enqueue(
        PostTicketUpdate(
            ticket_id=ticket.id,
            action=announce_as or action,
            actor=actor_username,
            assignee=assignee,
            resolution_notes=resolution_notes,
            points_charged=points_charged,
            reason=reason,
        )
    )


def _invite(ticket: Ticket, assignee: User, discord_id: int | None) -> None:
    """Queue adding the new assignee to the ticket's thread, if both exist on Discord."""
    if not ticket.discord_thread_id:
        return
    if discord_id is None:
        link = DiscordLink.objects.filter(user=assignee, is_active=True).first()
        discord_id = link.discord_id if link else None
    if discord_id:
        DiscordTask.enqueue(
            AddUserToThread(ticket_id=ticket.id, discord_id=discord_id, thread_id=ticket.discord_thread_id)
        )


def _give(
    ticket: Ticket,
    action: str,
    announce_as: str | None,
    actor_username: str,
    discord_id: int | None,
    discord_username: str | None,
    user: User | None,
) -> tuple[Ticket | None, str | None]:
    """Apply a claim or reassign transition to a locked ticket, for the user behind `user` or `discord_id`."""
    if error := refusal(ticket, action):
        return None, error

    assignee = get_user_for_ticket(discord_id=discord_id, user=user)
    if not assignee:
        return None, "Could not find a valid user to assign."

    if action == "claim":
        ticket.status = Ticket.STATUS_CLAIMED
        history_action = "claimed"
        details: dict[str, object] = {
            "claimed_by": actor_username,
            "discord_username": discord_username,
            "authentik_username": assignee.username,
        }
    else:
        history_action = "reassigned"
        details = {
            "reassigned_by": actor_username,
            "previous_assignee": ticket.assigned_to.username if ticket.assigned_to else None,
            "new_assignee": assignee.username,
        }
    ticket.assigned_to = assignee
    ticket.assigned_at = timezone.now()
    ticket.save()

    _record(
        ticket, history_action, assignee, actor_username, details, announce_as=announce_as, assignee=assignee.username
    )
    _invite(ticket, assignee, discord_id)
    return ticket, None


def claim_ticket(
    ticket_id: int,
    actor_username: str,
    discord_id: int | None = None,
    discord_username: str | None = None,
    user: User | None = None,
) -> tuple[Ticket | None, str | None]:
    """Claim an open ticket for the actor, identified by `user` or `discord_id`."""
    with transaction.atomic():
        ticket = _lock(ticket_id)
        if not ticket:
            return None, "Ticket not found."
        return _give(ticket, "claim", None, actor_username, discord_id, discord_username, user)


aclaim_ticket = _make_async(claim_ticket)


def assign_ticket(
    ticket_id: int,
    actor_username: str,
    discord_id: int | None = None,
    discord_username: str | None = None,
    user: User | None = None,
) -> tuple[Ticket | None, str | None]:
    """Give a ticket to someone else: an open one is claimed on their behalf; a claimed, resolved or
    cancelled one changes only its assignee (status, points and resolution are untouched)."""
    with transaction.atomic():
        ticket = _lock(ticket_id)
        if not ticket:
            return None, "Ticket not found."
        action = "claim" if ticket.status == Ticket.STATUS_OPEN else "reassign"
        return _give(ticket, action, "assigned", actor_username, discord_id, discord_username, user)


aassign_ticket = _make_async(assign_ticket)


def unclaim_ticket(
    ticket_id: int,
    actor_username: str,
    user: User | None = None,
) -> tuple[Ticket | None, str | None]:
    with transaction.atomic():
        ticket = _lock(ticket_id)
        if not ticket:
            return None, "Ticket not found."
        if error := refusal(ticket, "unclaim"):
            return None, error

        ticket.status = Ticket.STATUS_OPEN
        ticket.assigned_to = None
        ticket.assigned_at = None
        ticket.save()

        _record(ticket, "unclaimed", user, actor_username, {"unclaimed_by": actor_username})
        return ticket, None


aunclaim_ticket = _make_async(unclaim_ticket)


def resolve_ticket(
    ticket_id: int,
    actor_username: str,
    resolution_notes: str = "",
    points_override: int | None = None,
    discord_id: int | None = None,
    discord_username: str | None = None,
    user: User | None = None,
) -> tuple[Ticket | None, str | None]:
    """Resolve a ticket, charging the category's points or `points_override` (required for
    variable-point categories), and schedule its thread for archiving."""
    with transaction.atomic():
        ticket = _lock(ticket_id)
        if not ticket:
            return None, "Ticket not found."
        if error := refusal(ticket, "resolve"):
            return None, error

        cat_info = get_category_config(ticket.category_id) or {}
        if points_override is not None:
            if cat_info.get("variable_points", False):
                min_pts = int(cat_info.get("min_points", 0))
                max_pts = int(cat_info.get("max_points", 0))
                if points_override < min_pts:
                    return None, f"Point value must be at least {min_pts}."
                if max_pts and points_override > max_pts:
                    return None, f"Point value must be at most {max_pts}."
            point_penalty = points_override
        elif cat_info.get("variable_points", False):
            return None, "This category requires an explicit point value."
        else:
            point_penalty = cat_info.get("points", 0)

        resolver = get_user_for_ticket(discord_id=discord_id, user=user)

        ticket.status = Ticket.STATUS_RESOLVED
        ticket.resolved_at = timezone.now()
        ticket.resolved_by = resolver
        ticket.resolution_notes = resolution_notes
        ticket.points_charged = point_penalty
        if not ticket.assigned_to and resolver:
            ticket.assigned_to = resolver
        if ticket.discord_thread_id:
            ticket.thread_archive_scheduled_at = timezone.now() + THREAD_ARCHIVE_DELAY
        ticket.save()

        _record(
            ticket,
            "resolved",
            resolver,
            actor_username,
            {
                "resolved_by": actor_username,
                "discord_username": discord_username,
                "authentik_username": resolver.username if resolver else None,
                "notes": resolution_notes,
                "point_penalty": point_penalty,
            },
            resolution_notes=resolution_notes,
            points_charged=point_penalty,
        )
        return ticket, None


aresolve_ticket = _make_async(resolve_ticket)


def cancel_ticket(
    ticket_id: int,
    actor_username: str,
    user: User | None = None,
    reason: str = "",
    staff: bool = False,
) -> tuple[Ticket | None, str | None]:
    """Cancel a ticket with no point penalty and schedule its thread for archiving.

    Teams cancel only open tickets; staff (``staff=True``) may also cancel claimed ones.
    ``reason`` becomes the resolution notes (default "Cancelled by <actor>").
    """
    with transaction.atomic():
        ticket = _lock(ticket_id)
        if not ticket:
            return None, "Ticket not found."
        if error := refusal(ticket, "cancel"):
            return None, error
        if ticket.status == Ticket.STATUS_CLAIMED and not staff:
            return None, "Claimed tickets can only be cancelled by ticketing staff."

        ticket.status = Ticket.STATUS_CANCELLED
        ticket.resolved_at = timezone.now()
        ticket.resolution_notes = reason or f"Cancelled by {actor_username}"
        ticket.points_charged = 0
        if ticket.discord_thread_id:
            ticket.thread_archive_scheduled_at = timezone.now() + THREAD_ARCHIVE_DELAY
        ticket.save()

        _record(ticket, "cancelled", user, actor_username, {"cancelled_by": actor_username, "reason": reason})
        return ticket, None


acancel_ticket = _make_async(cancel_ticket)


def reopen_ticket(
    ticket_id: int,
    actor_username: str,
    reopen_reason: str = "",
    user: User | None = None,
) -> tuple[Ticket | None, str | None]:
    """Reopen a resolved ticket, unassigned, refunding its points until it is resolved again."""
    with transaction.atomic():
        ticket = _lock(ticket_id)
        if not ticket:
            return None, "Ticket not found."
        if error := refusal(ticket, "reopen"):
            return None, error
        if ticket.is_approved:
            return None, "Cannot reopen an approved ticket."

        old_assignee = ticket.assigned_to
        refunded_points = ticket.points_charged

        ticket.status = Ticket.STATUS_OPEN
        ticket.assigned_to = None
        ticket.resolved_at = None
        ticket.points_charged = 0
        ticket.save()

        details: dict[str, object] = {"reopened_by": actor_username, "refunded_points": refunded_points}
        if old_assignee:
            details["previous_assignee"] = old_assignee.username
        if reopen_reason:
            details["reason"] = reopen_reason

        _record(ticket, "reopened", user, actor_username, details, reason=reopen_reason)
        return ticket, None


areopen_ticket = _make_async(reopen_ticket)
