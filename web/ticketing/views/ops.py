import logging
from typing import cast

from django.contrib.auth.models import User
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.auth_utils import require_permission
from core.tickets_config import get_all_categories, get_category_config
from core.utils import by_approval, by_team, list_filters, optional_int
from team.models import Team
from ticketing.models import Ticket, TicketHistory
from ticketing.scoring_sync import recompute_team_ticket_adjustment

logger = logging.getLogger(__name__)


@require_permission(
    "ticketing_admin", "gold_team", error_message="Only Ticketing Admins or Gold Team can review tickets"
)
def ops_review_tickets(request: HttpRequest) -> HttpResponse:
    """Review resolved tickets for point approval."""
    from core.utils import filter_sort_paginate

    filters, filtered = list_filters(request, {"status": "pending", "team": "", "search": "", "category": ""})

    query = Ticket.objects.filter(status="resolved").select_related("team", "approved_by")

    query = by_team(by_approval(query, filters["status"]), filters["team"])

    if (category_id := optional_int(filters["category"])) is not None:
        query = query.filter(category_id=category_id)

    if filters["search"]:
        query = query.filter(
            Q(title__icontains=filters["search"])
            | Q(description__icontains=filters["search"])
            | Q(ticket_number__icontains=filters["search"])
            | Q(hostname__icontains=filters["search"])
            | Q(service_name__icontains=filters["search"])
        )

    result = filter_sort_paginate(
        request,
        query,
        sort_fields=["resolved_at", "points_charged", "team__team_number", "category"],
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
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
        "categories": get_all_categories(),
        "available_teams": Team.objects.filter(tickets__status="resolved").distinct().order_by("team_number"),
        "has_pending": Ticket.objects.filter(status="resolved", is_approved=False).exists(),
        "show_ops_nav": True,
    }

    if request.headers.get("HX-Request"):
        return render(request, "ticketing/review_tickets_table.html", context)

    return render(request, "ops_review_tickets.html", context)


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
