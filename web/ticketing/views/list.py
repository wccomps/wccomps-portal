import logging
from datetime import timedelta
from typing import cast

from django.contrib.auth.models import User
from django.db.models import Count, Max, Q
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from core.auth_utils import get_user_team, has_permission
from core.tickets_config import get_all_categories, get_category_config
from core.utils import optional_int
from ticketing.models import Ticket

logger = logging.getLogger(__name__)

VALID_PAGE_SIZES = [25, 50, 100, 200]
STALE_TICKET_MINUTES = 30


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
        return HttpResponseForbidden("You do not have permission to view tickets.")

    from core.utils import filter_sort_paginate

    # Several statuses allowed (#37): checkboxes send ?status=a&status=b, and sort/page links carry
    # them as one comma-separated value. Unknown values are ignored; none selected means all.
    requested = {part for value in request.GET.getlist("status") for part in value.split(",")}
    selected_statuses = [s for s, _label in Ticket.STATUS_CHOICES if s in requested]
    status_filter = ",".join(selected_statuses) or "all"
    category_filter = request.GET.get("category", "all") or "all"
    search_query = request.GET.get("search", "").strip()

    team_filter = request.GET.get("team", "") if is_ops else ""
    assignee_filter = request.GET.get("assignee", "") if is_ops else ""

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

    if (category_id := optional_int(category_filter)) is not None:
        query = query.filter(category_id=category_id)

    if search_query:
        query = query.filter(
            Q(title__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(ticket_number__icontains=search_query)
            | Q(hostname__icontains=search_query)
            | Q(service_name__icontains=search_query)
        )

    if (team_number := optional_int(team_filter)) is not None:
        query = query.filter(team__team_number=team_number)

    if assignee_filter == "unassigned":
        query = query.filter(assigned_to__isnull=True)
    elif (assignee_id := optional_int(assignee_filter)) is not None:
        query = query.filter(assigned_to_id=assignee_id)

    result = filter_sort_paginate(
        request,
        query,
        valid_sort_fields=[
            "created_at",
            "-created_at",
            "status",
            "-status",
            "team__team_number",
            "-team__team_number",
            "category",
            "-category",
            "assigned_to__username",
            "-assigned_to__username",
        ],
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

    is_ticketing_admin = has_permission(user, "ticketing_admin")

    context = {
        "is_ops": is_ops,
        "is_ticketing_admin": is_ticketing_admin,
        "team": team,
        "authentik_username": authentik_username,
        "page_obj": page_obj,
        "status_filter": status_filter,
        "selected_statuses": selected_statuses,
        "status_choices": Ticket.STATUS_CHOICES,
        "category_filter": category_filter,
        "search_query": search_query,
        "sort_by": sort_by,
        "page_size": page_size,
        "team_filter": team_filter,
        "assignee_filter": assignee_filter,
        "assignees": assignees,
        "categories": get_all_categories(),
    }

    if request.headers.get("HX-Request"):
        return render(request, "ticketing/ticket_list_table.html", context)

    return render(request, "ticket_list.html", context)


def ticket_notifications(request: HttpRequest) -> JsonResponse:
    """JSON endpoint for ticket notification polling (ops staff only)."""
    user = cast(User, request.user)
    if not (
        has_permission(user, "admin")
        or has_permission(user, "ticketing_support")
        or has_permission(user, "ticketing_admin")
    ):
        return JsonResponse({"error": "forbidden"}, status=403)

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
