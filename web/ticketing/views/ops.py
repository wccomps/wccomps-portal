import logging
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from core.auth_utils import require_permission
from core.tickets_config import get_all_categories, get_category_config
from team.models import Team
from ticketing.forms import TicketVerifyForm
from ticketing.models import Ticket, TicketHistory
from ticketing.scoring_sync import recompute_team_ticket_adjustment

logger = logging.getLogger(__name__)


@require_permission(
    "ticketing_admin", "gold_team", error_message="Only Ticketing Admins or Gold Team can review tickets"
)
def ops_review_tickets(request: HttpRequest) -> HttpResponse:
    """Review resolved tickets for point approval."""
    from core.utils import filter_sort_paginate

    status_filter = request.GET.get("status", "pending") or "pending"
    team_filter = request.GET.get("team", "")
    search_query = request.GET.get("search", "").strip()
    category_filter = request.GET.get("category", "")

    query = Ticket.objects.filter(status="resolved").select_related("team", "approved_by")

    if status_filter == "approved":
        query = query.filter(is_approved=True)
    elif status_filter == "pending":
        query = query.filter(is_approved=False)

    if category_filter:
        query = query.filter(category_id=int(category_filter))

    if team_filter:
        try:
            team_number = int(team_filter)
            query = query.filter(team__team_number=team_number)
        except ValueError:
            pass

    if search_query:
        query = query.filter(
            Q(title__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(ticket_number__icontains=search_query)
            | Q(hostname__icontains=search_query)
            | Q(service_name__icontains=search_query)
        )

    result = filter_sort_paginate(
        request,
        query,
        valid_sort_fields=[
            "resolved_at",
            "-resolved_at",
            "points_charged",
            "-points_charged",
            "team__team_number",
            "-team__team_number",
            "category",
            "-category",
        ],
        default_sort="-resolved_at",
    )
    page_obj = result["page_obj"]
    sort_by = result["current_sort"]

    # Enrich only the current page with category info
    enriched = []
    for ticket in page_obj.object_list:
        cat_info = get_category_config(ticket.category_id) or {}
        enriched.append(
            {
                "ticket": ticket,
                "category_name": cat_info.get("display_name", "Unknown"),
                "expected_points": cat_info.get("points", 0),
            }
        )
    page_obj.object_list = enriched  # type: ignore[assignment]  # templates iterate enriched dicts, keep pagination

    context = {
        "page_obj": page_obj,
        "status_filter": status_filter,
        "team_filter": team_filter,
        "category_filter": category_filter,
        "search_query": search_query,
        "sort_by": sort_by,
        "categories": get_all_categories(),
        "has_pending": Ticket.objects.filter(status="resolved", is_approved=False).exists(),
        "show_ops_nav": True,
    }

    if request.headers.get("HX-Request"):
        return render(request, "cotton/review_tickets_table.html", context)

    return render(request, "ops_review_tickets.html", context)


@require_permission(
    "ticketing_admin", "gold_team", error_message="Only Ticketing Admins or Gold Team can verify tickets"
)
def ops_verify_ticket(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Verify ticket points (admin only)."""
    if request.method != "POST":
        return HttpResponse("Method not allowed", status=405)

    user = cast(User, request.user)
    authentik_username = user.username

    try:
        ticket = Ticket.objects.select_related("team").get(ticket_number=ticket_number)
    except Ticket.DoesNotExist:
        return HttpResponse("Ticket not found", status=404)

    if ticket.status != "resolved":
        messages.error(request, f"Cannot verify - ticket is {ticket.status}, must be resolved")
        return redirect("ticket_detail", ticket_number=ticket_number)

    form = TicketVerifyForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid points value. Must be a number.")
        return redirect("ticket_detail", ticket_number=ticket_number)

    points_adjustment = form.cleaned_data.get("points_adjustment")
    approval_notes = form.cleaned_data.get("verification_notes", "")

    if points_adjustment is not None:
        ticket.points_charged = points_adjustment

    ticket.is_approved = True
    ticket.approved_by = user
    ticket.approved_at = timezone.now()
    ticket.approval_notes = approval_notes
    ticket.save()

    TicketHistory.objects.create(
        ticket=ticket,
        action="points_verified",
        details={
            "verified_by": authentik_username,
            "points_charged": ticket.points_charged,
            "approval_notes": approval_notes,
        },
    )

    recompute_team_ticket_adjustment(ticket.team)

    logger.info(f"Ticket {ticket_number} points verified by {authentik_username}: {ticket.points_charged} points")

    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("ops_review_tickets")


@require_permission(
    "ticketing_admin", "gold_team", error_message="Only Ticketing Admins or Gold Team can batch verify tickets"
)
@require_http_methods(["POST"])
def ops_batch_verify_tickets(request: HttpRequest) -> HttpResponse:
    """Bulk approve selected ticket points."""
    user = cast(User, request.user)
    authentik_username = user.username
    now = timezone.now()
    approved_team_ids: set[int] = set()

    def approve(ticket: Ticket) -> None:
        ticket.is_approved = True
        ticket.approved_by = user
        ticket.approved_at = now
        ticket.save()
        approved_team_ids.add(ticket.team_id)
        TicketHistory.objects.create(
            ticket=ticket,
            action="points_verified",
            details={
                "verified_by": authentik_username,
                "points_charged": ticket.points_charged,
                "batch": True,
            },
        )

    from core.utils import bulk_approve

    result = bulk_approve(
        request,
        field_name="ticket_ids",
        queryset=Ticket.objects.filter(is_approved=False, status="resolved"),
        redirect_url="ops_review_tickets",
        item_label="ticket",
        on_item=approve,
    )
    for team in Team.objects.filter(pk__in=approved_team_ids):
        recompute_team_ticket_adjustment(team)
    logger.info(f"Batch verified tickets by {authentik_username}")
    return result
