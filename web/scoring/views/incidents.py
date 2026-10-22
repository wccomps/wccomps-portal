from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from core.auth_utils import get_user_team, has_permission, require_permission, team_record_or_404
from core.stored_files import file_response
from core.utils import by_approval, by_team, chosen_team, list_filters, team_numbers
from team.models import Team

from ..calculator import calculate_suggested_recovery_points, suggest_red_score_matches
from ..forms import IncidentMatchForm, IncidentReportForm
from ..models import IncidentReport, IncidentScreenshot
from ..quotient_sync import get_box_metadata
from ..screenshots import ScreenshotError, read_screenshots

# Who sees and reviews every team's incident reports: White Team (the permission's groups hold Gold Team and admins)
INCIDENT_STAFF = "white_team"


@transaction.atomic
def submit_incident_report(request: HttpRequest) -> HttpResponse:
    """Submit incident report (blue team or admin)."""

    user = cast(User, request.user)
    is_admin = has_permission(user, INCIDENT_STAFF)
    team: Team | None = None

    if not is_admin:
        team = get_user_team(user)
        if not team:
            raise PermissionDenied("You must be assigned to a team to submit incident reports.")

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
    """List incident reports (all reports for incident staff, or the user's team reports)."""
    user = cast(User, request.user)

    if has_permission(user, INCIDENT_STAFF):
        incidents = IncidentReport.objects.all()
    elif user_team := get_user_team(user):
        incidents = IncidentReport.objects.filter(team=user_team)
    else:
        raise PermissionDenied
    incidents = incidents.select_related("team", "submitted_by").order_by("-created_at")

    context = {
        "incidents": incidents,
    }
    return render(request, "scoring/incident_list.html", context)


def view_incident_report(request: HttpRequest, incident_id: int) -> HttpResponse:
    user = cast(User, request.user)
    incident = team_record_or_404(IncidentReport.objects.all(), user, staff=(INCIDENT_STAFF,), id=incident_id)

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
    user = cast(User, request.user)
    incident = team_record_or_404(IncidentReport.objects.all(), user, staff=(INCIDENT_STAFF,), id=incident_id)

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
    screenshot = team_record_or_404(
        IncidentScreenshot.objects.select_related("incident"),
        cast(User, request.user),
        staff=(INCIDENT_STAFF,),
        team_field="incident__team",
        id=screenshot_id,
    )
    if not screenshot.file_data:
        raise Http404  # the file was lost

    return file_response(bytes(screenshot.file_data), screenshot.filename, inline=True)


@require_permission(INCIDENT_STAFF, error_message="Only Gold Team or White Team members can review incident reports")
def review_incidents(request: HttpRequest) -> HttpResponse:
    """Review and match incident reports (White Team, which holds Gold Team and admins)."""
    from core.utils import filter_sort_paginate

    filters, filtered = list_filters(request, {"status": "pending", "team": "", "box": "", "search": ""})

    base_query = IncidentReport.objects.select_related("team").prefetch_related("screenshots")

    team = chosen_team(filters["team"])
    base_query = by_team(by_approval(base_query, filters["status"]), team)

    if filters["box"]:
        base_query = base_query.filter(affected_boxes__contains=[filters["box"]])

    if filters["search"]:
        from django.db.models import Q

        base_query = base_query.filter(
            Q(attack_description__icontains=filters["search"]) | Q(affected_service__icontains=filters["search"])
        )

    result = filter_sort_paginate(
        request,
        base_query,
        sort_fields=["created_at", "team__team_number", "attack_detected_at"],
        default_sort="-created_at",
    )
    page_obj = result["page_obj"]
    sort_by = result["current_sort"]

    context = {
        "page_obj": page_obj,
        "team_numbers": team_numbers(Team.objects.filter(incident_reports__isnull=False), team),
        "chosen_team": team,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
        "has_pending": IncidentReport.objects.filter(is_approved=False).exists(),
    }

    if request.headers.get("HX-Request"):
        return render(request, "scoring/partials/review_incidents_table.html", context)

    return render(request, "scoring/review_incidents.html", context)


@require_permission(INCIDENT_STAFF, error_message="Only Gold Team or White Team members can approve incidents")
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


@require_permission(INCIDENT_STAFF, error_message="Only Gold Team or White Team members can match incident reports")
@transaction.atomic
def match_incident(request: HttpRequest, incident_id: int) -> HttpResponse:
    """Match an incident report to a red team finding."""
    incident = get_object_or_404(IncidentReport, id=incident_id)

    suggested_findings = suggest_red_score_matches(incident)

    if request.method == "POST":
        form = IncidentMatchForm(suggested_findings, request.POST, instance=incident)

        if form.is_valid():
            incident = form.save()
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
