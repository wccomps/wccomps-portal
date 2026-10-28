from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from core.auth_utils import has_permission, require_permission
from core.errors import wants_json_error
from core.stored_files import file_response
from core.utils import optional_int
from team.models import Team

from ..forms import RedTeamScoreForm
from ..models import (
    AttackType,
    RedTeamScore,
    RedTeamScreenshot,
)
from ..quotient_sync import get_box_metadata
from ..screenshots import ScreenshotError, read_screenshots


def findings_list(request: HttpRequest, *, default_status: str, page_size: int) -> dict[str, object]:
    """The filtered, sorted page of findings and the filter state for the table both findings pages show."""
    from django.db.models import Q

    from core.utils import by_approval, by_team, chosen_team, filter_sort_paginate, list_filters, team_numbers

    filters, filtered = list_filters(
        request, {"status": default_status, "team": "", "attack_type": "", "submitter": "", "search": ""}
    )

    findings = RedTeamScore.objects.prefetch_related("affected_teams", "screenshots").select_related(
        "attack_type", "submitted_by"
    )
    team = chosen_team(filters["team"])
    findings = by_team(by_approval(findings, filters["status"]), team, "affected_teams")
    if (attack_type_id := optional_int(filters["attack_type"])) is not None:
        findings = findings.filter(attack_type_id=attack_type_id)
    if (submitter_id := optional_int(filters["submitter"])) is not None:
        findings = findings.filter(submitted_by_id=submitter_id)
    if filters["search"]:
        findings = findings.filter(
            Q(attack_type__name__icontains=filters["search"])
            | Q(notes__icontains=filters["search"])
            | Q(affected_service__icontains=filters["search"])
        )

    # distinct: the team filter joins the many-to-many affected_teams
    result = filter_sort_paginate(
        request,
        findings.distinct(),
        sort_fields=("attack_type__name", "created_at"),  # the findings table's sortable columns
        default_sort="-created_at",
        page_size=page_size,
    )
    return {
        "findings": result["page_obj"],
        "page_obj": result["page_obj"],
        "sort_by": result["current_sort"],
        "filters": filters,
        "filtered": filtered,
        "team_numbers": team_numbers(Team.objects.filter(red_team_scores__isnull=False), team),
        "chosen_team": team,
        "available_attack_types": AttackType.objects.filter(findings__isnull=False).distinct().order_by("name"),
        "available_submitters": User.objects.filter(red_scores_submitted__isnull=False).distinct().order_by("username"),
    }


@require_permission(
    "red_team",
    "gold_team",
    error_message="Only Red Team or Gold Team members can review findings",
)
def red_team_findings(request: HttpRequest) -> HttpResponse:
    """Review page for red team findings."""
    context = {
        **findings_list(request, default_status="pending", page_size=50),
        "pending_findings": RedTeamScore.objects.filter(is_approved=False).exists(),
        # A Red Team reader's rows link to the Red Team page: the review page is Gold Team's
        "reviewing": has_permission(cast(User, request.user), "gold_team"),
    }
    if request.headers.get("HX-Request"):
        return render(request, "scoring/partials/red_findings_table.html", context)
    return render(request, "scoring/review_red_findings.html", context)


@require_permission(
    "red_team",
    error_message="Only Red Team members can view findings",
)
def red_team_scores(request: HttpRequest) -> HttpResponse:
    """Red team view of all findings (read-only, can delete own)."""
    context = findings_list(request, default_status="all", page_size=25)
    if request.headers.get("HX-Request"):
        return render(request, "scoring/partials/red_findings_table.html", context)
    return render(request, "scoring/red_team_findings.html", context)


@require_permission("gold_team", error_message="Only Gold Team members can approve findings")
@transaction.atomic
@require_http_methods(["POST"])
def bulk_approve_red_scores(request: HttpRequest) -> HttpResponse:
    """Bulk approve red team findings; red submits, gold approves."""
    from core.utils import bulk_approve

    user = cast(User, request.user)
    return bulk_approve(
        request,
        field_name="finding_ids",
        queryset=RedTeamScore.objects.filter(is_approved=False),
        redirect_url="scoring:red_team_findings",
        item_label="finding",
        on_item=lambda item: item.approve(user),
    )


@require_permission("red_team", error_message="Only Red Team members can submit findings")
@transaction.atomic
def submit_red_score(request: HttpRequest) -> HttpResponse:
    from ..models import RedTeamIPPool

    user = cast(User, request.user)
    user_pools = RedTeamIPPool.objects.filter(created_by=user)

    if request.method == "POST":
        form = RedTeamScoreForm(request.POST, request.FILES, user=user)

        if form.is_valid():
            try:
                screenshots = read_screenshots(request.FILES.getlist("screenshots"))
            except ScreenshotError as e:
                messages.error(request, f"File upload failed: {e}")
            else:
                return _create_red_score(request, form, user, screenshots)
    else:
        form = RedTeamScoreForm(user=user)

    context = {
        "form": form,
        "box_metadata": get_box_metadata(),
        "user_pools": user_pools,
    }
    return render(request, "scoring/submit_red_finding.html", context)


def _create_red_score(
    request: HttpRequest, form: RedTeamScoreForm, user: User, screenshots: list[tuple[bytes, str, str]]
) -> HttpResponse:
    finding = form.save(commit=False)
    finding.submitted_by = user
    finding.save()
    form.save_m2m()

    for file_data, filename, mime_type in screenshots:
        RedTeamScreenshot.objects.create(finding=finding, file_data=file_data, filename=filename, mime_type=mime_type)

    messages.success(request, f"Finding #{finding.id} created successfully.")
    return redirect("scoring:red_team_scores")


def _finding(finding_id: int) -> RedTeamScore:
    return get_object_or_404(
        RedTeamScore.objects.select_related(
            "attack_type", "submitted_by", "approved_by", "source_ip_pool"
        ).prefetch_related("affected_teams", "screenshots"),
        id=finding_id,
    )


@require_permission("red_team", "gold_team", error_message="Only Red Team or Gold Team can view findings")
def view_red_score(request: HttpRequest, finding_id: int) -> HttpResponse:
    """A finding's Red Team page; a Gold Team reviewer's page for it is the review page."""
    user = cast(User, request.user)
    if not has_permission(user, "red_team"):
        return redirect("scoring:review_red_finding", finding_id=finding_id)
    finding = _finding(finding_id)
    context = {"finding": finding, "can_delete": finding.submitted_by == user and not finding.is_approved}
    return render(request, "scoring/view_red_finding.html", context)


@require_permission("gold_team", error_message="Only Gold Team members can review findings")
def review_red_finding(request: HttpRequest, finding_id: int) -> HttpResponse:
    return render(request, "scoring/review_red_finding.html", {"finding": _finding(finding_id)})


@require_permission("red_team", error_message="Only Red Team members can delete findings")
@transaction.atomic
@require_http_methods(["POST"])
def delete_red_score(request: HttpRequest, finding_id: int) -> HttpResponse:
    """Delete a red team finding (owner only, before approval)."""
    finding = get_object_or_404(RedTeamScore, id=finding_id)
    user = cast(User, request.user)

    if finding.submitted_by != user:
        messages.error(request, "You can only delete your own findings")
        return redirect("scoring:red_team_scores")

    if finding.is_approved:
        messages.error(request, "Cannot delete a finding that has already been approved")
        return redirect("scoring:red_team_scores")

    finding_num = finding.id
    finding.delete()
    messages.success(request, f"Red team finding #{finding_num} deleted")
    return redirect("scoring:red_team_scores")


@require_permission("red_team", "gold_team", error_message="Only Red Team or Gold Team can view this")
def red_screenshot_download(request: HttpRequest, screenshot_id: int) -> HttpResponse:
    from django.http import Http404

    screenshot = get_object_or_404(RedTeamScreenshot, id=screenshot_id)

    if not screenshot.file_data:
        raise Http404("File data not available (file was lost)")

    return file_response(bytes(screenshot.file_data), screenshot.filename, inline=True)


@require_permission("red_team", error_message="Only Red Team members can manage IP pools")
def ip_pool_list(request: HttpRequest) -> HttpResponse:
    from ..models import RedTeamIPPool

    user = cast(User, request.user)
    pools = RedTeamIPPool.objects.filter(created_by=user)

    context = {
        "pools": pools,
    }
    return render(request, "scoring/ip_pool_list.html", context)


@require_permission("red_team", error_message="Only Red Team members can manage IP pools")
@transaction.atomic
def ip_pool_create(request: HttpRequest) -> HttpResponse:
    from ..forms import RedTeamIPPoolForm

    user = cast(User, request.user)

    if request.method == "POST":
        form = RedTeamIPPoolForm(request.POST, user=user)
        if form.is_valid():
            pool = form.save(commit=False)
            pool.created_by = user
            pool.save()
            messages.success(request, f"IP pool '{pool.name}' created with {pool.ip_count} IPs")
            # The finding form's pool modal asks for JSON
            if wants_json_error(request):
                return JsonResponse(
                    {
                        "success": True,
                        "pool": {
                            "id": pool.id,
                            "name": pool.name,
                            "ip_count": pool.ip_count,
                        },
                    }
                )
            return redirect("scoring:ip_pool_list")
        else:
            if wants_json_error(request):
                return JsonResponse(
                    {"success": False, "error": "The pool wasn't created.", "errors": form.errors}, status=400
                )
    else:
        form = RedTeamIPPoolForm(user=user)

    context = {
        "form": form,
        "action": "Create",
    }
    return render(request, "scoring/ip_pool_form.html", context)


@require_permission("red_team", error_message="Only Red Team members can manage IP pools")
@transaction.atomic
def ip_pool_edit(request: HttpRequest, pool_id: int) -> HttpResponse:
    from ..forms import RedTeamIPPoolForm
    from ..models import RedTeamIPPool

    user = cast(User, request.user)
    pool = get_object_or_404(RedTeamIPPool, id=pool_id, created_by=user)

    if request.method == "POST":
        form = RedTeamIPPoolForm(request.POST, instance=pool, user=user)
        if form.is_valid():
            pool = form.save()
            messages.success(request, f"IP pool '{pool.name}' updated")
            return redirect("scoring:ip_pool_list")
    else:
        form = RedTeamIPPoolForm(instance=pool, user=user)

    context = {
        "form": form,
        "pool": pool,
        "action": "Edit",
    }
    return render(request, "scoring/ip_pool_form.html", context)


@require_permission("red_team", error_message="Only Red Team members can manage IP pools")
@transaction.atomic
@require_http_methods(["POST"])
def ip_pool_delete(request: HttpRequest, pool_id: int) -> HttpResponse:
    from ..models import RedTeamIPPool

    user = cast(User, request.user)
    pool = get_object_or_404(RedTeamIPPool, id=pool_id, created_by=user)

    if pool.findings.exists():
        messages.error(request, f"Cannot delete pool '{pool.name}': it is used by {pool.findings.count()} finding(s)")
        return redirect("scoring:ip_pool_list")

    pool_name = pool.name
    pool.delete()
    messages.success(request, f"IP pool '{pool_name}' deleted")
    return redirect("scoring:ip_pool_list")
