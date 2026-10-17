import logging
from datetime import timedelta
from typing import cast
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from core.auth_utils import get_user_team, has_permission, require_permission
from core.errors import json_endpoint
from core.tickets_config import get_all_categories, get_category_config
from core.utils import by_team, chosen_team, list_filters, optional_int, team_numbers
from team.models import Team
from ticketing.models import Ticket

logger = logging.getLogger(__name__)

VALID_PAGE_SIZES = [25, 50, 100, 200]
STALE_TICKET_MINUTES = 30


def back_to_list(request: HttpRequest, *also: str) -> HttpResponse:
    """Back to the page the request came from (its Referer) when that is the ticket list, as the person had it
    filtered, or one of the also paths; else to the plain list.

    The ticket pages' own way back is utils.js's sessionStorage copy of the list's query.
    """
    list_path = reverse("ticket_list")
    referer = request.headers.get("referer", "")
    came_from = urlsplit(referer)
    if url_has_allowed_host_and_scheme(referer, {request.get_host()}) and came_from.path in (list_path, *also):
        return redirect(f"{came_from.path}?{came_from.query}" if came_from.query else came_from.path)
    return redirect(list_path)


def ticket_list(request: HttpRequest) -> HttpResponse:
    """Unified ticket list view for both team members and ops staff."""
    user = cast(User, request.user)
    authentik_username = user.username

    is_ops = (
        has_permission(user, "ticketing_support")
        or has_permission(user, "ticketing_admin")
        or has_permission(user, "admin")
    )
    team = get_user_team(user)

    if not is_ops and not team:
        raise PermissionDenied("You do not have permission to view tickets.")

    from core.utils import filter_sort_paginate

    # Several statuses allowed (#37): checkboxes send ?status=a&status=b; a comma-separated value
    # (an older link's) works too. Unknown values are ignored; none selected means all.
    requested = {part for value in request.GET.getlist("status") for part in value.split(",")}
    selected_statuses = [s for s, _label in Ticket.STATUS_CHOICES if s in requested]
    filters, filtered = list_filters(
        request, {"category": "all", "search": ""} | ({"team": "", "assignee": ""} if is_ops else {})
    )
    filtered = filtered or bool(selected_statuses)

    page_size = optional_int(request.GET.get("page_size", ""))
    if page_size not in VALID_PAGE_SIZES:
        page_size = 50

    if is_ops:
        query = (
            Ticket.objects.select_related("team")
            .exclude(ticket_number="")
            .annotate(
                comment_count=Count("comments", distinct=True),
                attachment_count=Count("attachments", distinct=True),
                last_activity=Max("history__timestamp"),
            )
        )
    else:
        query = (
            Ticket.objects.select_related("team")
            .filter(team=team)
            .annotate(
                comment_count=Count("comments", distinct=True),
                attachment_count=Count("attachments", distinct=True),
                last_activity=Max("history__timestamp"),
            )
        )

    if selected_statuses:
        query = query.filter(status__in=selected_statuses)

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

    team_chosen = chosen_team(filters.get("team", ""))
    query = by_team(query, team_chosen)

    if filters.get("assignee", "") == "unassigned":
        query = query.filter(assigned_to__isnull=True)
    elif (assignee_id := optional_int(filters.get("assignee", ""))) is not None:
        query = query.filter(assigned_to_id=assignee_id)

    result = filter_sort_paginate(
        request,
        query,
        sort_fields=["created_at", "status", "team__team_number", "category", "assigned_to__username"],
        default_sort="-created_at",
        page_size=page_size,
    )
    page_obj = result["page_obj"]
    sort_by = result["current_sort"]

    # Enrich only the current page with category info and stale status
    thirty_minutes_ago = timezone.now() - timedelta(minutes=STALE_TICKET_MINUTES)
    enriched = []
    for ticket in page_obj.object_list:
        cat_info = get_category_config(ticket.category_id) or {}
        is_stale = (
            is_ops
            and ticket.status == "claimed"
            and ticket.assigned_at is not None
            and ticket.assigned_at < thirty_minutes_ago
        )
        enriched.append(
            {
                "ticket": ticket,
                "category_name": cat_info.get("display_name", "Unknown"),
                "is_stale": is_stale,
                "status_display": ticket.status.upper().replace("_", " "),
            }
        )
    page_obj.object_list = enriched  # type: ignore[assignment]  # templates iterate enriched dicts, keep pagination

    assignees = User.objects.filter(assigned_tickets__isnull=False).distinct().order_by("username") if is_ops else []
    teams = team_numbers(Team.objects.filter(tickets__isnull=False), team_chosen) if is_ops else []

    is_ticketing_admin = has_permission(user, "ticketing_admin")

    context = {
        "is_ops": is_ops,
        "is_ticketing_admin": is_ticketing_admin,
        "team": team,
        "authentik_username": authentik_username,
        "page_obj": page_obj,
        "selected_statuses": selected_statuses,
        "status_choices": Ticket.STATUS_CHOICES,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
        "page_size": page_size,
        "assignees": assignees,
        "team_numbers": teams,
        "chosen_team": team_chosen,
        "categories": get_all_categories(),
    }

    if request.headers.get("HX-Request"):
        return render(request, "ticketing/ticket_list_table.html", context)

    return render(request, "ticket_list.html", context)


@json_endpoint
@require_permission("admin", "ticketing_support", "ticketing_admin")
def ticket_notifications(request: HttpRequest) -> JsonResponse:
    """JSON endpoint for ticket notification polling (ops staff only)."""
    since_id = optional_int(request.GET.get("since_id", "")) or 0

    open_count = Ticket.objects.filter(status="open").count()
    raw_tickets = (
        Ticket.objects.filter(status="open", id__gt=since_id)
        .order_by("id")
        .values("id", "ticket_number", "title", "category_id")[:10]
    )

    new_tickets = []
    for t in raw_tickets:
        cat_config = get_category_config(t["category_id"])
        new_tickets.append(
            {
                "id": t["id"],
                "number": t["ticket_number"],
                "title": t["title"],
                "category_display": cat_config["display_name"] if cat_config else "Unknown",
            }
        )

    return JsonResponse({"open_count": open_count, "new_tickets": new_tickets})
