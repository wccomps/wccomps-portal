import csv
import json
from datetime import timedelta
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.auth_utils import has_permission, require_permission
from orange_team.forms import AssignmentRejectForm, CheckAssignForm, FollowUpForm, OrangeCheckForm, extract_criteria
from orange_team.models import (
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
    OrangeCheckIn,
    OrangeFollowUp,
)
from orange_team.services import auto_assign_check, create_orange_score_from_assignment, update_check_criteria
from team.models import Team


@require_permission(
    "orange_team", "gold_team", "orange_team_lead", error_message="Only Orange Team members can access this page"
)
def dashboard(request: HttpRequest) -> HttpResponse:
    """Orange team dashboard showing check-in status and assignments."""
    user = cast(User, request.user)

    active_checkin = OrangeCheckIn.objects.filter(user=user, is_active=True).first()

    my_assignments = (
        OrangeAssignment.objects.filter(user=user)
        .exclude(status__in=["approved", "rejected"])
        .select_related("orange_check", "team")
        .prefetch_related("results__criterion")
        .order_by("orange_check__title", "team__team_number")
    )

    followups = OrangeFollowUp.objects.filter(user=user, dismissed=False).select_related(
        "assignment__orange_check", "assignment__team"
    )

    is_lead = has_permission(user, "orange_team_lead")
    context: dict[str, object] = {
        "active_checkin": active_checkin,
        "assignments": my_assignments,
        "followups": followups,
        "is_lead": is_lead,
    }

    return render(request, "orange_team/dashboard.html", context)


@require_permission(
    "orange_team", "gold_team", "orange_team_lead", error_message="Only Orange Team members can access this page"
)
def toggle_checkin(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("orange_team:dashboard")
    user = cast(User, request.user)
    active = OrangeCheckIn.objects.filter(user=user, is_active=True).first()
    if active:
        active.is_active = False
        active.checked_out_at = timezone.now()
        active.save()
    else:
        OrangeCheckIn.objects.create(user=user)
    return redirect("orange_team:dashboard")


@require_permission("orange_team_lead",error_message="Only leads can manage check-ins")
def admin_toggle_checkin(request: HttpRequest, user_id: int) -> HttpResponse:
    """Toggle check-in/out for another user (lead only)."""
    if request.method != "POST":
        return redirect("orange_team:team_checkins")
    target_user = User.objects.get(pk=user_id)
    active = OrangeCheckIn.objects.filter(user=target_user, is_active=True).first()
    if active:
        active.is_active = False
        active.checked_out_at = timezone.now()
        active.save()
    else:
        OrangeCheckIn.objects.create(user=target_user)
    return redirect("orange_team:team_checkins")


@require_permission("orange_team_lead",error_message="Only leads can view team check-ins")
def team_checkins(request: HttpRequest) -> HttpResponse:
    """Lead view showing all checked-in orange team members."""
    checked_in_members = OrangeCheckIn.objects.filter(is_active=True).select_related("user")
    return render(
        request,
        "orange_team/checkins.html",
        {"checked_in_members": checked_in_members, "is_lead": True},
    )


@require_permission("orange_team_lead",error_message="Only leads can review assignments")
def review_queue(request: HttpRequest) -> HttpResponse:
    """Lead view showing submitted assignments awaiting review."""
    review_assignments = (
        OrangeAssignment.objects.filter(status__in=["submitted", "in_progress"])
        .select_related("orange_check", "team", "user")
        .order_by("-submitted_at")
    )
    return render(
        request,
        "orange_team/review.html",
        {"review_assignments": review_assignments, "is_lead": True},
    )


@require_permission("orange_team_lead",error_message="Only leads can manage checks")
def check_list(request: HttpRequest) -> HttpResponse:
    checks = (
        OrangeCheck.objects.annotate(
            criteria_count=Count("criteria"),
            assignment_count=Count("assignments"),
        )
        .select_related("created_by")
        .order_by("-created_at")
    )
    return render(request, "orange_team/check_list.html", {"checks": checks, "is_lead": True})


@require_permission("orange_team_lead",error_message="Only leads can manage checks")
def check_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = OrangeCheckForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Title is required.")
            return render(request, "orange_team/check_form.html", {"mode": "create", "is_lead": True})

        title = form.cleaned_data["title"]
        description = form.cleaned_data["description"]
        scheduled_at = form.cleaned_data["scheduled_at"] or None
        max_points = form.cleaned_data.get("max_points") or 0
        minutes = form.cleaned_data.get("time_limit_minutes")
        time_limit = timedelta(minutes=minutes) if minutes else None

        criteria = extract_criteria(request.POST)

        user = cast(User, request.user)
        with transaction.atomic():
            orange_check = OrangeCheck.objects.create(
                title=title,
                description=description,
                scheduled_at=scheduled_at,
                max_points=max_points,
                time_limit=time_limit,
                created_by=user,
            )
            for c in criteria:
                OrangeCheckCriterion.objects.create(
                    orange_check=orange_check,
                    label=c["label"],
                    points=c["points"],
                    sort_order=c["sort_order"],
                )

        messages.success(request, f"Check '{title}' created with {len(criteria)} criteria.")
        return redirect("orange_team:check_list")

    return render(request, "orange_team/check_form.html", {"mode": "create", "is_lead": True})


@require_permission("orange_team_lead",error_message="Only leads can manage checks")
def check_detail(request: HttpRequest, check_id: int) -> HttpResponse:
    orange_check = get_object_or_404(
        OrangeCheck.objects.prefetch_related("criteria", "assignments__user", "assignments__team"),
        pk=check_id,
    )
    checked_in_users = User.objects.filter(orange_checkins__is_active=True).distinct()
    return render(
        request,
        "orange_team/check_detail.html",
        {
            "orange_check": orange_check,
            "checked_in_users": checked_in_users,
            "is_lead": True,
        },
    )


@require_permission("orange_team_lead",error_message="Only leads can manage checks")
def check_edit(request: HttpRequest, check_id: int) -> HttpResponse:
    orange_check = get_object_or_404(OrangeCheck, pk=check_id)

    if request.method == "POST":
        form = OrangeCheckForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Title is required.")
            return render(
                request,
                "orange_team/check_form.html",
                {"mode": "edit", "orange_check": orange_check, "is_lead": True},
            )

        title = form.cleaned_data["title"]
        description = form.cleaned_data["description"]
        scheduled_at = form.cleaned_data["scheduled_at"] or None
        max_points = form.cleaned_data.get("max_points") or 0
        minutes = form.cleaned_data.get("time_limit_minutes")
        time_limit = timedelta(minutes=minutes) if minutes else None

        criteria_data = extract_criteria(request.POST)

        with transaction.atomic():
            orange_check.title = title
            orange_check.description = description
            orange_check.scheduled_at = scheduled_at
            orange_check.max_points = max_points
            orange_check.time_limit = time_limit
            orange_check.save()
            update_check_criteria(orange_check, criteria_data)

        messages.success(request, f"Check '{title}' updated.")
        return redirect("orange_team:check_detail", check_id=orange_check.pk)

    existing_criteria = list(orange_check.criteria.values("id", "label", "points"))
    time_limit_minutes = int(orange_check.time_limit.total_seconds() // 60) if orange_check.time_limit else ""
    return render(
        request,
        "orange_team/check_form.html",
        {
            "mode": "edit",
            "orange_check": orange_check,
            "existing_criteria": existing_criteria,
            "time_limit_minutes": time_limit_minutes,
            "is_lead": True,
        },
    )


@require_permission("orange_team_lead",error_message="Only leads can manage checks")
def check_duplicate(request: HttpRequest, check_id: int) -> HttpResponse:
    """Duplicate a check and its criteria into a new draft."""
    if request.method != "POST":
        return redirect("orange_team:check_detail", check_id=check_id)
    original = get_object_or_404(OrangeCheck.objects.prefetch_related("criteria"), pk=check_id)
    user = cast(User, request.user)
    with transaction.atomic():
        new_check = OrangeCheck.objects.create(
            title=f"{original.title} (copy)",
            description=original.description,
            created_by=user,
            status="draft",
        )
        for criterion in original.criteria.all():
            OrangeCheckCriterion.objects.create(
                orange_check=new_check,
                label=criterion.label,
                points=criterion.points,
                sort_order=criterion.sort_order,
            )
    messages.success(request, f"Duplicated '{original.title}' as new draft.")
    return redirect("orange_team:check_detail", check_id=new_check.pk)


@require_permission("orange_team_lead",error_message="Only leads can manage checks")
def check_assign(request: HttpRequest, check_id: int) -> HttpResponse:
    """Assign checked-in users to score teams for a check."""
    if request.method != "POST":
        return redirect("orange_team:check_detail", check_id=check_id)

    orange_check = get_object_or_404(OrangeCheck.objects.prefetch_related("criteria"), pk=check_id)
    checked_in = User.objects.filter(orange_checkins__is_active=True).distinct()
    form = CheckAssignForm(request.POST, choices=[(u.pk, u.username) for u in checked_in])
    if not form.is_valid():
        messages.error(request, "Select at least one user to assign.")
        return redirect("orange_team:check_detail", check_id=check_id)

    users = list(User.objects.filter(pk__in=form.cleaned_data["user_ids"]))
    active_teams = list(Team.objects.filter(is_active=True).order_by("team_number"))

    if not active_teams:
        messages.error(request, "No active teams found.")
        return redirect("orange_team:check_detail", check_id=check_id)

    auto_assign_check(orange_check, users, active_teams)
    orange_check.status = "active"
    orange_check.save(update_fields=["status"])

    messages.success(request, f"Assigned {len(active_teams)} teams across {len(users)} users.")
    return redirect("orange_team:check_detail", check_id=check_id)


def assignment_save(request: HttpRequest, assignment_id: int) -> HttpResponse:
    """Autosave a criterion result for an assignment."""
    if not (
        has_permission(request.user, "orange_team")
        or has_permission(request.user, "gold_team")
        or has_permission(request.user, "orange_team_lead")
    ):
        return JsonResponse({"error": "Access denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    user = cast(User, request.user)
    assignment = get_object_or_404(OrangeAssignment, pk=assignment_id, user=user)

    if assignment.status in ("submitted", "approved"):
        return JsonResponse({"error": "Assignment already submitted"}, status=400)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid data"}, status=400)

    if "score" in data:
        if assignment.orange_check.criteria.exists():
            return JsonResponse({"error": "This check uses criteria"}, status=400)
        try:
            score = int(data["score"])
        except TypeError, ValueError:
            return JsonResponse({"error": "Invalid score"}, status=400)
        max_score = assignment.orange_check.max_score
        if score < 0 or score > max_score:
            return JsonResponse({"error": "Score out of range"}, status=400)
        assignment.score = score
        if assignment.status == "pending":
            assignment.status = "in_progress"
        assignment.save()
        return JsonResponse({"score": score, "max_score": max_score})

    try:
        criterion_id = data["criterion_id"]
        met = data["met"]
    except KeyError:
        return JsonResponse({"error": "Invalid data"}, status=400)

    result = get_object_or_404(OrangeAssignmentResult, assignment=assignment, criterion_id=criterion_id)
    result.met = met
    result.save()

    if assignment.status == "pending":
        assignment.status = "in_progress"
        assignment.save()

    score = assignment.calculate_score()
    max_score = assignment.orange_check.max_score
    return JsonResponse({"score": score, "max_score": max_score})


@require_permission(
    "orange_team", "gold_team", "orange_team_lead", error_message="Only Orange Team members can access this page"
)
def assignment_submit(request: HttpRequest, assignment_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("orange_team:dashboard")

    user = cast(User, request.user)
    assignment = get_object_or_404(OrangeAssignment, pk=assignment_id, user=user)

    if assignment.status in ("submitted", "approved"):
        messages.error(request, "Assignment already submitted.")
        return redirect("orange_team:dashboard")

    if assignment.orange_check.criteria.exists():
        assignment.score = assignment.calculate_score()
    # criteria-free checks keep the directly-entered assignment.score
    assignment.status = "submitted"
    assignment.submitted_at = timezone.now()
    assignment.save()

    messages.success(
        request,
        f"Assignment submitted: {assignment.orange_check.title}"
        f" - Team {assignment.team.team_number}"
        f" ({assignment.score}/{assignment.orange_check.max_score})",
    )
    return redirect("orange_team:dashboard")


@require_permission(
    "orange_team", "gold_team", "orange_team_lead", error_message="Only Orange Team members can access this page"
)
def followup_create(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("orange_team:dashboard")

    user = cast(User, request.user)
    form = FollowUpForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid form data.")
        return redirect("orange_team:dashboard")

    minutes = form.cleaned_data["minutes"]
    note = form.cleaned_data["note"]
    assignment = get_object_or_404(OrangeAssignment, pk=form.cleaned_data["assignment_id"], user=user)
    OrangeFollowUp.objects.create(
        user=user,
        assignment=assignment,
        remind_at=timezone.now() + timedelta(minutes=minutes),
        note=note,
    )
    messages.success(request, f"Reminder set for {minutes} minutes.")
    return redirect("orange_team:dashboard")


@require_permission(
    "orange_team", "gold_team", "orange_team_lead", error_message="Only Orange Team members can access this page"
)
def followup_dismiss(request: HttpRequest, followup_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("orange_team:dashboard")

    user = cast(User, request.user)
    followup = get_object_or_404(OrangeFollowUp, pk=followup_id, user=user)
    followup.dismissed = True
    followup.save()
    return redirect("orange_team:dashboard")


@require_permission("orange_team_lead",error_message="Only leads can approve assignments")
def assignment_approve(request: HttpRequest, assignment_id: int) -> HttpResponse:
    """Approve a submitted orange team check, creating an OrangeTeamScore record."""
    if request.method != "POST":
        return redirect("orange_team:review_queue")

    user = cast(User, request.user)
    # Locked so a double submit or a racing reject can't act on the same submission twice.
    with transaction.atomic():
        assignment = get_object_or_404(
            OrangeAssignment.objects.select_for_update(of=("self",)).select_related("orange_check", "team", "user"),
            pk=assignment_id,
        )

        if assignment.status != "submitted":
            messages.error(request, "Only submitted assignments can be approved.")
            return redirect("orange_team:review_queue")

        assignment.status = "approved"
        assignment.reviewed_by = user
        assignment.reviewed_at = timezone.now()
        assignment.save()

        create_orange_score_from_assignment(assignment, user)

    messages.success(
        request,
        f"Approved: {assignment.orange_check.title} - Team {assignment.team.team_number} ({assignment.score} pts)",
    )
    return redirect("orange_team:review_queue")


@require_permission("orange_team_lead",error_message="Only leads can reject assignments")
def assignment_reject(request: HttpRequest, assignment_id: int) -> HttpResponse:
    """Reject a submitted assignment, sending it back to the teamer."""
    if request.method != "POST":
        return redirect("orange_team:review_queue")

    user = cast(User, request.user)
    form = AssignmentRejectForm(request.POST)
    form.is_valid()  # Always valid (optional field)
    with transaction.atomic():
        assignment = get_object_or_404(
            OrangeAssignment.objects.select_for_update(of=("self",)).select_related("orange_check", "team"),
            pk=assignment_id,
        )

        if assignment.status != "submitted":
            messages.error(request, "Only submitted assignments can be rejected.")
            return redirect("orange_team:review_queue")

        assignment.status = "rejected"
        assignment.reviewed_by = user
        assignment.reviewed_at = timezone.now()
        assignment.notes = form.cleaned_data.get("notes", "")
        assignment.save()

    messages.success(
        request,
        f"Rejected: {assignment.orange_check.title} - Team {assignment.team.team_number}",
    )
    return redirect("orange_team:review_queue")


@require_permission("orange_team_lead",error_message="Only leads can export scores")
def export_scores(request: HttpRequest) -> HttpResponse:
    assignments = (
        OrangeAssignment.objects.filter(status__in=["submitted", "approved"])
        .select_related("orange_check", "team", "user", "reviewed_by")
        .order_by("orange_check__title", "team__team_number")
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orange_checks.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "Check",
            "Team Number",
            "Team Name",
            "Assignee",
            "Score",
            "Max Score",
            "Status",
            "Submitted At",
            "Reviewed By",
            "Reviewed At",
        ]
    )
    for a in assignments:
        writer.writerow(
            [
                a.orange_check.title,
                a.team.team_number,
                a.team.team_name,
                a.user.username,
                a.score or 0,
                a.orange_check.max_score,
                a.status,
                a.submitted_at.strftime("%Y-%m-%d %H:%M") if a.submitted_at else "",
                a.reviewed_by.username if a.reviewed_by else "",
                a.reviewed_at.strftime("%Y-%m-%d %H:%M") if a.reviewed_at else "",
            ]
        )
    return response
