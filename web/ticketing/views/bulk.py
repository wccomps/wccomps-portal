import logging
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_POST

from core.auth_utils import has_permission, require_permission
from ticketing.forms import TicketBulkActionForm
from ticketing.lifecycle import claim_ticket, resolve_ticket
from ticketing.models import Ticket

from .list import back_to_list

logger = logging.getLogger(__name__)


@require_POST
@require_permission("ticketing_support")
def tickets_bulk_claim(request: HttpRequest) -> HttpResponse:
    """Bulk claim tickets (ticketing staff only)."""
    user = cast(User, request.user)
    form = TicketBulkActionForm(request.POST)
    if not form.is_valid():
        messages.info(request, str(form.errors["ticket_numbers"][0]))
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


@require_POST
@require_permission("ticketing_support")
def tickets_bulk_resolve(request: HttpRequest) -> HttpResponse:
    """Bulk resolve tickets at their category's points; same rule as resolving one ticket."""
    user = cast(User, request.user)
    form = TicketBulkActionForm(request.POST)
    if not form.is_valid():
        messages.info(request, str(form.errors["ticket_numbers"][0]))
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
