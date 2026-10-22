import logging
from typing import cast

from django.contrib.auth.models import User
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from core.auth_utils import require_permission
from core.utils import by_approval, by_team, chosen_team, list_filters, optional_int, team_numbers
from team.models import Team
from ticketing.models import Ticket, TicketCategory, TicketHistory

logger = logging.getLogger(__name__)


@require_permission(
    "ticketing_admin", "gold_team", error_message="Only Ticketing Admins or Gold Team can review tickets"
)
def ops_review_tickets(request: HttpRequest) -> HttpResponse:
    """Review resolved tickets for point approval."""
    from core.utils import filter_sort_paginate

    filters, filtered = list_filters(request, {"status": "pending", "team": "", "search": "", "category": ""})

    query = Ticket.objects.filter(status="resolved").select_related("team", "approved_by", "category")

    team = chosen_team(filters["team"])
    query = by_team(by_approval(query, filters["status"]), team)

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
    enriched = [
        {
            "ticket": ticket,
            "category_name": ticket.category.display_name if ticket.category else "Unknown",
            "expected_points": ticket.category.points if ticket.category else 0,
        }
        for ticket in page_obj.object_list
    ]
    page_obj.object_list = enriched  # type: ignore[assignment]  # templates iterate enriched dicts, keep pagination

    context = {
        "page_obj": page_obj,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
        "categories": TicketCategory.objects.all(),
        "team_numbers": team_numbers(Team.objects.filter(tickets__status="resolved"), team),
        "chosen_team": team,
        "has_pending": Ticket.objects.filter(status="resolved", is_approved=False).exists(),
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

    def approve(ticket: Ticket) -> None:
        ticket.approve(user)
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
    logger.info(f"Batch verified tickets by {authentik_username}")
    return result
