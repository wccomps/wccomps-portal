from typing import cast

from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.auth_utils import require_permission
from core.utils import by_approval, by_team, chosen_team, list_filters, optional_int, team_numbers

from ..models import OrangeTeamScore


def orange_team_redirect(request: HttpRequest) -> HttpResponse:
    return redirect("orange_team:dashboard")


@require_permission("orange_team", error_message="Only Orange Team or Gold Team members can review checks")
def review_orange(request: HttpRequest) -> HttpResponse:
    from django.db.models import Q
    from orange_team.models import OrangeCheck

    from core.utils import filter_sort_paginate
    from team.models import Team

    filters, filtered = list_filters(request, {"status": "pending", "team": "", "check": "", "search": ""})

    base_query = OrangeTeamScore.objects.select_related("team", "submitted_by", "approved_by", "orange_check")

    team = chosen_team(filters["team"])
    base_query = by_team(by_approval(base_query, filters["status"]), team)

    if filters["check"]:
        if filters["check"] == "manual":
            base_query = base_query.filter(orange_check__isnull=True)
        elif (check_id := optional_int(filters["check"])) is not None:
            base_query = base_query.filter(orange_check_id=check_id)

    if filters["search"]:
        base_query = base_query.filter(Q(description__icontains=filters["search"]))

    result = filter_sort_paginate(
        request,
        base_query,
        sort_fields=["created_at", "team__team_number", "points_awarded"],
        default_sort="-created_at",
    )
    page_obj = result["page_obj"]
    sort_by = result["current_sort"]

    available_checks = OrangeCheck.objects.filter(orange_scores__isnull=False).distinct().order_by("title")

    context = {
        "page_obj": page_obj,
        "has_pending": OrangeTeamScore.objects.filter(is_approved=False).exists(),
        "team_numbers": team_numbers(Team.objects.filter(orange_team_scores__isnull=False), team),
        "chosen_team": team,
        "available_checks": available_checks,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
    }

    if request.headers.get("HX-Request"):
        return render(request, "scoring/partials/review_orange_table.html", context)

    return render(request, "scoring/review_orange.html", context)


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
