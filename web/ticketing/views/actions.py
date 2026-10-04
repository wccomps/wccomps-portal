import logging
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.auth_utils import get_authentik_id, get_user_team, has_permission
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
from ticketing.scoring_sync import recompute_team_ticket_adjustment

logger = logging.getLogger(__name__)


@require_POST
def ticket_cancel(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Cancel an open ticket (team members only)."""
    user = cast(User, request.user)
    authentik_username = user.username
    team = get_user_team(user)

    if not team:
        return render(
            request,
            "error.html",
            {
                "error": "Invalid account",
                "message": "Your account is not associated with a team.",
            },
        )

    try:
        ticket_obj = Ticket.objects.select_related("team").get(ticket_number=ticket_number, team=team)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {
                "error": "Ticket not found",
                "message": f"Ticket {ticket_number} does not exist or does not belong to your team.",
            },
        )

    ticket, error = cancel_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to cancel ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    recompute_team_ticket_adjustment(ticket.team)
    logger.info(f"Ticket {ticket_number} cancelled by {authentik_username} via web")

    return redirect("ticket_list")


@require_POST
def ticket_claim(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Claim a ticket (ticketing staff only)."""
    user = cast(User, request.user)
    authentik_username = user.username
    get_authentik_id(user)

    if not (has_permission(user, "ticketing_support") or has_permission(user, "ticketing_admin")):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to perform this action."},
            status=403,
        )

    try:
        ticket_obj = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {"error": "Not Found", "message": "The requested ticket was not found."},
            status=404,
        )

    ticket, error = claim_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to claim ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} claimed by {authentik_username}")
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("ticket_list")


@require_POST
def ticket_unclaim(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Unclaim a ticket (ticketing staff only)."""
    user = cast(User, request.user)
    authentik_username = user.username

    if not (has_permission(user, "ticketing_support") or has_permission(user, "ticketing_admin")):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to perform this action."},
            status=403,
        )

    try:
        ticket_obj = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {"error": "Not Found", "message": "The requested ticket was not found."},
            status=404,
        )

    is_admin = has_permission(user, "ticketing_admin")
    has_claimed = ticket_obj.assigned_to and ticket_obj.assigned_to.username == authentik_username

    if not is_admin and not has_claimed:
        messages.error(request, "You can only unclaim tickets you have claimed")
        return redirect("ticket_detail", ticket_number=ticket_number)

    ticket, error = unclaim_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to unclaim ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} unclaimed by {authentik_username}")
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("ticket_list")


@require_POST
def ticket_reassign(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Reassign a ticket to another support member (operations team only)."""
    user = cast(User, request.user)
    authentik_username = user.username

    if not (has_permission(user, "ticketing_support") or has_permission(user, "ticketing_admin")):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to perform this action."},
            status=403,
        )

    try:
        ticket_obj = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {"error": "Not Found", "message": "The requested ticket was not found."},
            status=404,
        )

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
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to assign ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} reassigned to {new_assignee_username} by {authentik_username}")
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("ticket_list")


@require_POST
def ticket_resolve(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Resolve a ticket (operations team only)."""
    user = cast(User, request.user)
    authentik_username = user.username
    get_authentik_id(user)

    is_ticketing_admin = has_permission(user, "ticketing_admin")

    if not (has_permission(user, "ticketing_support") or is_ticketing_admin):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to perform this action."},
            status=403,
        )

    try:
        ticket_obj = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {"error": "Not Found", "message": "The requested ticket was not found."},
            status=404,
        )

    # Verify ownership: only the assigned user or admins can resolve
    assigned_username = ticket_obj.assigned_to.username if ticket_obj.assigned_to else None
    if not is_ticketing_admin and assigned_username != authentik_username:
        return render(
            request,
            "error.html",
            {
                "error": "Access Denied",
                "message": "Only the assigned support member or administrators can resolve this ticket.",
            },
            status=403,
        )

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
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("ticket_list")


@require_POST
def ticket_reopen(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Reopen a resolved ticket (operations team only)."""
    user = cast(User, request.user)
    authentik_username = user.username

    if not has_permission(user, "ticketing_admin"):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to perform this action."},
            status=403,
        )

    try:
        ticket_obj = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {"error": "Not Found", "message": "The requested ticket was not found."},
            status=404,
        )

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

    recompute_team_ticket_adjustment(ticket.team)
    logger.info(
        f"Ticket {ticket_number} reopened by {authentik_username}" + (f": {reopen_reason}" if reopen_reason else "")
    )
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("ticket_list")


@require_POST
def ticket_change_category(request: HttpRequest, ticket_number: str) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    if not has_permission(user, "ticketing_support"):
        return render(
            request,
            "error.html",
            {"error": "Access Denied", "message": "You do not have permission to perform this action."},
            status=403,
        )

    try:
        ticket = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return render(
            request,
            "error.html",
            {"error": "Not Found", "message": "The requested ticket was not found."},
            status=404,
        )

    is_admin = has_permission(user, "ticketing_admin")
    has_claimed = ticket.assigned_to and ticket.assigned_to.username == authentik_username

    if not is_admin and not has_claimed:
        messages.error(request, "You must claim the ticket first")
        return redirect("ticket_detail", ticket_number=ticket_number)

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
