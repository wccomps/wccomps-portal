import logging
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.auth_utils import require_permission, team_record_or_404
from ticketing.access import TICKET_STAFF
from ticketing.forms import TicketChangeCategoryForm, TicketReassignForm, TicketReopenForm, TicketResolveForm
from ticketing.lifecycle import (
    assign_ticket,
    cancel_ticket,
    claim_ticket,
    reopen_ticket,
    resolve_ticket,
    unclaim_ticket,
)
from ticketing.models import Ticket

from .list import back_to_list

logger = logging.getLogger(__name__)


@require_POST
def ticket_cancel(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Cancel an open ticket (team members only)."""
    user = cast(User, request.user)
    authentik_username = user.username
    # A team's own: staff file and work tickets but don't cancel them
    ticket_obj = team_record_or_404(Ticket.objects.select_related("team"), user, staff=(), ticket_number=ticket_number)

    ticket, error = cancel_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to cancel ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} cancelled by {authentik_username} via web")

    return redirect("ticket_list")


@require_POST
@require_permission(TICKET_STAFF)
def ticket_claim(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Claim a ticket (ticketing staff only)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    ticket, error = claim_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to claim ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} claimed by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_unclaim(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Put a claimed ticket back in the open queue (its assignee or a ticketing admin: the lifecycle checks)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    ticket, error = unclaim_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to unclaim ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} unclaimed by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_reassign(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Give a ticket to a staff member: any ticketing staff for an open one, its assignee or a ticketing admin for a
    ticket someone holds (the lifecycle checks)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketReassignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "New assignee username is required")
        return redirect("ticket_detail", ticket_number=ticket_number)
    new_assignee_username = form.cleaned_data["new_assignee_username"]

    new_assignee_user = User.objects.filter(username=new_assignee_username).first()
    if not new_assignee_user:
        messages.error(request, f"User '{new_assignee_username}' not found")
        return redirect("ticket_detail", ticket_number=ticket_number)

    ticket, error = assign_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=new_assignee_user,
        actor=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to assign ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} reassigned to {new_assignee_username} by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_resolve(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Resolve a ticket (its assignee or a ticketing admin: the lifecycle checks)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketResolveForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid form data. Check points value.")
        return redirect("ticket_detail", ticket_number=ticket_number)

    resolution_notes = form.cleaned_data["resolution_notes"]
    points_override = form.cleaned_data.get("points_override")

    ticket, error = resolve_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        resolution_notes=resolution_notes,
        points_override=points_override,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to resolve ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} resolved by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission("ticketing_admin")
def ticket_reopen(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Reopen a resolved ticket (ticketing admins)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketReopenForm(request.POST)
    form.is_valid()  # Always valid (optional field)
    reopen_reason = form.cleaned_data.get("reopen_reason", "")

    ticket, error = reopen_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        reopen_reason=reopen_reason,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to reopen ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(
        f"Ticket {ticket_number} reopened by {authentik_username}" + (f": {reopen_reason}" if reopen_reason else "")
    )
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_change_category(request: HttpRequest, ticket_number: str) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    ticket = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketChangeCategoryForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid category")
        return redirect("ticket_detail", ticket_number=ticket_number)
    new_category_id = form.cleaned_data["new_category"]

    from ticketing.utils import change_ticket_category_atomic

    changed, error = change_ticket_category_atomic(
        ticket_id=ticket.id, new_category_id=new_category_id, actor_username=authentik_username, user=user
    )
    if error or changed is None:
        messages.error(request, error or "Failed to change category")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} category changed by {authentik_username} to category {new_category_id}")

    return redirect("ticket_detail", ticket_number=ticket_number)
