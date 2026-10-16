import logging
from typing import cast
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from core.auth_utils import has_permission
from ticketing.forms import TicketBulkActionForm
from ticketing.lifecycle import claim_ticket, resolve_ticket
from ticketing.models import Ticket

logger = logging.getLogger(__name__)


def back_to_list(request: HttpRequest) -> HttpResponse:
    """The ticket list as the person had it filtered (the page the bulk action came from), else the plain list."""
    list_path = reverse("ticket_list")
    referer = request.headers.get("referer", "")
    came_from = urlsplit(referer)
    if url_has_allowed_host_and_scheme(referer, {request.get_host()}) and came_from.path == list_path:
        return redirect(f"{list_path}?{came_from.query}" if came_from.query else list_path)
    return redirect(list_path)


def tickets_bulk_claim(request: HttpRequest) -> HttpResponse:
    """Bulk claim tickets (ticketing staff only)."""
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    user = cast(User, request.user)
    if not has_permission(user, "ticketing_support"):
        return HttpResponse("Access denied", status=403)

    form = TicketBulkActionForm(request.POST)
    if not form.is_valid():
        messages.info(request, "No tickets selected")
        return back_to_list(request)

    claimed = 0
    for ticket_id in Ticket.objects.filter(ticket_number__in=form.cleaned_data["ticket_numbers"]).values_list(
        "id", flat=True
    ):
        ticket, error = claim_ticket(ticket_id=ticket_id, actor_username=user.username, user=user)
        if ticket is not None and not error:
            claimed += 1

    logger.info(f"Bulk claimed {claimed} tickets by {user.username}")
    return back_to_list(request)


def tickets_bulk_resolve(request: HttpRequest) -> HttpResponse:
    """Bulk resolve tickets at their category's points; same rule as resolving one ticket."""
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    user = cast(User, request.user)
    if not has_permission(user, "ticketing_support"):
        return HttpResponse("Access denied", status=403)

    form = TicketBulkActionForm(request.POST)
    if not form.is_valid():
        messages.info(request, "No tickets selected")
        return back_to_list(request)

    requested = Ticket.objects.filter(ticket_number__in=form.cleaned_data["ticket_numbers"])
    tickets = requested if has_permission(user, "ticketing_admin") else requested.filter(assigned_to=user)

    resolved = 0
    skipped = [
        f"{number} (not assigned to you)"
        for number in requested.exclude(pk__in=tickets.values("pk")).values_list("ticket_number", flat=True)
    ]
    for ticket_id, ticket_number in tickets.values_list("id", "ticket_number"):
        ticket, error = resolve_ticket(
            ticket_id=ticket_id,
            actor_username=user.username,
            resolution_notes="Bulk resolved via web interface",
            user=user,
        )
        if ticket is None or error:
            skipped.append(f"{ticket_number} ({error})")
            continue
        resolved += 1

    if skipped:
        messages.warning(request, "Not resolved: " + "; ".join(skipped))
    logger.info(f"Bulk resolved {resolved} tickets by {user.username}")
    return back_to_list(request)
