from typing import cast

from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.auth_utils import require_permission
from core.utils import filter_number

from ..models import OrangeTeamScore


def orange_team_redirect(request: HttpRequest) -> HttpResponse:
    return redirect("orange_team:dashboard")


@require_permission("orange_team", error_message="Only Orange Team or Gold Team members can review checks")
def review_orange(request: HttpRequest) -> HttpResponse:
    from django.db.models import Q
    from orange_team.models import OrangeCheck

    from core.utils import filter_sort_paginate
    from team.models import Team

    status_filter = request.GET.get("status", "pending") or "pending"
    team_filter = request.GET.get("team", "")
    check_filter = request.GET.get("check", "")
    search_query = request.GET.get("search", "").strip()

    base_query = OrangeTeamScore.objects.select_related("team", "submitted_by", "approved_by", "orange_check")

    if status_filter == "pending":
        base_query = base_query.filter(is_approved=False)
    elif status_filter == "approved":
        base_query = base_query.filter(is_approved=True)

    if (team_id := filter_number(team_filter)) is not None:
        base_query = base_query.filter(team__id=team_id)

    if check_filter:
        if check_filter == "manual":
            base_query = base_query.filter(orange_check__isnull=True)
        elif (check_id := filter_number(check_filter)) is not None:
            base_query = base_query.filter(orange_check_id=check_id)

    if search_query:
        base_query = base_query.filter(Q(description__icontains=search_query))

    result = filter_sort_paginate(
        request,
        base_query,
        valid_sort_fields=[
            "created_at",
            "-created_at",
            "team__team_number",
            "-team__team_number",
            "points_awarded",
            "-points_awarded",
        ],
        default_sort="-created_at",
    )
    page_obj = result["page_obj"]
    sort_by = result["current_sort"]

    total_checks = OrangeTeamScore.objects.count()
    pending_count = OrangeTeamScore.objects.filter(is_approved=False).count()
    approved_count = total_checks - pending_count

    available_teams = Team.objects.filter(orange_team_scores__isnull=False).distinct().order_by("team_number")
    available_checks = OrangeCheck.objects.filter(orange_scores__isnull=False).distinct().order_by("title")

    context = {
        "page_obj": page_obj,
        "total_checks": total_checks,
        "pending_count": pending_count,
        "approved_count": approved_count,
        "has_pending": pending_count > 0,
        "available_teams": available_teams,
        "available_checks": available_checks,
        "selected_team": team_filter,
        "selected_check": check_filter,
        "search_query": search_query,
        "status_filter": status_filter,
        "sort_by": sort_by,
    }

    if request.headers.get("HX-Request"):
        return render(request, "cotton/review_orange_table.html", context)

    return render(request, "scoring/review_orange.html", context)


def submit_orange_check(request: HttpRequest) -> HttpResponse:
    """Redirect to the orange team dashboard."""
    return redirect("orange_team:dashboard")


@require_permission("orange_team", error_message="Only Orange Team or Gold Team members can approve checks")
@transaction.atomic
@require_http_methods(["POST"])
def bulk_approve_orange_adjustments(request: HttpRequest) -> HttpResponse:
    from core.utils import bulk_approve

    user = cast(User, request.user)
    return bulk_approve(
        request,
        field_name="adjustment_ids",
        queryset=OrangeTeamScore.objects.filter(is_approved=False),
        redirect_url="scoring:review_orange",
        item_label="check",
        on_item=lambda item: item.approve(user),
    )
