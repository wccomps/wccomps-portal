from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from core.auth_utils import get_user_team, has_permission, require_permission
from team.models import Team

from ..calculator import calculate_suggested_recovery_points, suggest_red_score_matches
from ..forms import IncidentMatchForm, IncidentReportForm
from ..models import IncidentReport, IncidentScreenshot
from ..quotient_sync import get_box_metadata
from ..screenshots import ScreenshotError, read_screenshots, screenshot_response


@transaction.atomic
def submit_incident_report(request: HttpRequest) -> HttpResponse:
    """Submit incident report (blue team or admin)."""

    user = cast(User, request.user)
    is_admin = has_permission(user, "gold_team") or has_permission(user, "white_team")
    team: Team | None = None

    if not is_admin:
        team = get_user_team(user)
        if not team:
            messages.error(request, "You must be assigned to a team to submit incident reports")
            return redirect("leaderboard_page")

    if request.method == "POST":
        form = IncidentReportForm(team, is_admin, request.POST, request.FILES)

        if form.is_valid():
            try:
                screenshots = read_screenshots(request.FILES.getlist("screenshots"))
            except ScreenshotError as e:
                messages.error(request, f"File upload failed: {e}")
            else:
                incident = form.save(commit=False)

                if is_admin:
                    incident.team = form.cleaned_data["team"]
                elif team is not None:
                    incident.team = team
                else:
                    # This should never happen due to earlier validation
                    messages.error(request, "Team assignment error")
                    return redirect("leaderboard_page")

                incident.submitted_by = user
                incident.save()

                for file_data, filename, mime_type in screenshots:
                    IncidentScreenshot.objects.create(
                        incident=incident, file_data=file_data, filename=filename, mime_type=mime_type
                    )

                messages.success(request, f"Incident report #{incident.id} submitted successfully")
                return redirect("scoring:view_incident_report", incident_id=incident.id)
    else:
        form = IncidentReportForm(team, is_admin)

    context = {
        "form": form,
        "team": team,
        "is_admin": is_admin,
        "box_metadata": get_box_metadata(),
    }
    return render(request, "scoring/submit_incident.html", context)


def incident_list(request: HttpRequest) -> HttpResponse:
    """List all incidents for the user's team (blue team view)."""
    user = cast(User, request.user)

    if has_permission(user, "gold_team") or has_permission(user, "white_team"):
        incidents = IncidentReport.objects.all().select_related("team", "submitted_by").order_by("-created_at")
    else:
        user_team = get_user_team(user)
        if not user_team:
            return render(
                request,
                "error.html",
                {"error": "Access Denied", "message": "You do not have permission to access this page."},
                status=403,
            )

        incidents = (
            IncidentReport.objects.filter(team=user_team).select_related("team", "submitted_by").order_by("-created_at")
        )

    context = {
        "incidents": incidents,
        "current_user": user,
    }
    return render(request, "scoring/incident_list.html", context)


def view_incident_report(request: HttpRequest, incident_id: int) -> HttpResponse:
    incident = get_object_or_404(IncidentReport, id=incident_id)

    user = cast(User, request.user)
    if not has_permission(user, "gold_team") and not has_permission(user, "white_team"):
        user_team = get_user_team(user)
        if not user_team or incident.team != user_team:
            messages.error(request, "You do not have permission to view this incident report")
            return redirect("leaderboard_page")

    can_delete = incident.submitted_by == user and not incident.is_approved

    context = {
        "incident": incident,
        "can_delete": can_delete,
    }
    return render(request, "scoring/view_incident.html", context)


@transaction.atomic
@require_http_methods(["POST"])
def delete_incident_report(request: HttpRequest, incident_id: int) -> HttpResponse:
    """Delete an incident report (owner only, before review)."""
    incident = get_object_or_404(IncidentReport, id=incident_id)
    user = cast(User, request.user)

    if incident.submitted_by != user:
        messages.error(request, "You can only delete your own incident reports")
        return redirect("scoring:view_incident_report", incident_id=incident_id)

    if incident.is_approved:
        messages.error(request, "Cannot delete an incident report that has already been reviewed")
        return redirect("scoring:view_incident_report", incident_id=incident_id)

    incident_num = incident.id
    incident.delete()
    messages.success(request, f"Incident report #{incident_num} deleted")
    return redirect("scoring:incident_list")


def incident_screenshot_download(request: HttpRequest, screenshot_id: int) -> HttpResponse:
    from django.http import Http404

    screenshot = get_object_or_404(IncidentScreenshot, id=screenshot_id)

    # Gold and White Team see every screenshot; a team sees its own incidents'
    user = cast(User, request.user)
    if not has_permission(user, "gold_team") and not has_permission(user, "white_team"):
        user_team = get_user_team(user)
        if not user_team or screenshot.incident.team != user_team:
            return HttpResponseForbidden("You do not have permission to view this file")

    if not screenshot.file_data:
        raise Http404("File data not available (file was lost)")

    return screenshot_response(bytes(screenshot.file_data), screenshot.filename)


@require_permission(
    "gold_team", "white_team", error_message="Only Gold Team or White Team members can review incident reports"
)
def review_incidents(request: HttpRequest) -> HttpResponse:
    """Review and match incident reports (gold team)."""
    from core.utils import filter_sort_paginate

    status_filter = request.GET.get("status", "pending") or "pending"
    team_filter = request.GET.get("team", "")
    box_filter = request.GET.get("box", "")
    search_query = request.GET.get("search", "").strip()

    base_query = IncidentReport.objects.select_related("team").prefetch_related("screenshots")

    if status_filter == "pending":
        base_query = base_query.filter(is_approved=False)
    elif status_filter == "approved":
        base_query = base_query.filter(is_approved=True)

    if team_filter:
        base_query = base_query.filter(team__id=team_filter)

    if box_filter:
        base_query = base_query.filter(affected_boxes__contains=[box_filter])

    if search_query:
        from django.db.models import Q

        base_query = base_query.filter(
            Q(attack_description__icontains=search_query) | Q(affected_service__icontains=search_query)
        )

    result = filter_sort_paginate(
        request,
        base_query,
        valid_sort_fields=[
            "created_at",
            "-created_at",
            "team__team_number",
            "-team__team_number",
            "attack_detected_at",
            "-attack_detected_at",
        ],
        default_sort="-created_at",
    )
    page_obj = result["page_obj"]
    sort_by = result["current_sort"]

    # Stats (unfiltered counts)
    total_incidents = IncidentReport.objects.count()
    approved_count = IncidentReport.objects.filter(is_approved=True).count()
    pending_count = total_incidents - approved_count

    available_teams = Team.objects.filter(incident_reports__isnull=False).distinct().order_by("team_number")

    context = {
        "page_obj": page_obj,
        "total_incidents": total_incidents,
        "approved_count": approved_count,
        "pending_count": pending_count,
        "available_teams": available_teams,
        "selected_team": team_filter,
        "selected_box": box_filter,
        "search_query": search_query,
        "status_filter": status_filter,
        "sort_by": sort_by,
        "has_pending": pending_count > 0,
    }

    if request.headers.get("HX-Request"):
        return render(request, "cotton/review_incidents_table.html", context)

    return render(request, "scoring/review_incidents.html", context)


@require_permission(
    "gold_team", "white_team", error_message="Only Gold Team or White Team members can approve incidents"
)
@transaction.atomic
@require_http_methods(["POST"])
def bulk_approve_incidents(request: HttpRequest) -> HttpResponse:
    from core.utils import bulk_approve

    user = cast(User, request.user)
    return bulk_approve(
        request,
        field_name="incident_ids",
        queryset=IncidentReport.objects.filter(is_approved=False),
        redirect_url="scoring:review_incidents",
        item_label="incident",
        on_item=lambda item: item.approve(user),
    )


@require_permission(
    "gold_team", "white_team", error_message="Only Gold Team or White Team members can match incident reports"
)
@transaction.atomic
def match_incident(request: HttpRequest, incident_id: int) -> HttpResponse:
    """Match incident to red team finding (gold team)."""
    incident = get_object_or_404(IncidentReport, id=incident_id)

    suggested_findings = suggest_red_score_matches(incident)

    if request.method == "POST":
        form = IncidentMatchForm(suggested_findings, request.POST, instance=incident)

        if form.is_valid():
            incident = form.save(commit=False)
            incident.approve(cast(User, request.user))

            messages.success(request, f"Incident #{incident.id} reviewed and {incident.points_returned} points awarded")
            return redirect("scoring:review_incidents")
    else:
        if suggested_findings:
            suggested_points = calculate_suggested_recovery_points(incident, suggested_findings[0])
            form = IncidentMatchForm(
                suggested_findings, instance=incident, initial={"points_returned": suggested_points}
            )
        else:
            form = IncidentMatchForm(suggested_findings, instance=incident)

    context = {
        "incident": incident,
        "form": form,
        "suggested_findings": suggested_findings,
    }
    return render(request, "scoring/match_incident.html", context)
