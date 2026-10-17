import json
from datetime import timedelta
from typing import cast

from django import forms
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.auth_utils import has_permission, require_permission
from core.csv_export import csv_writer
from orange_team.forms import (
    MAX_FOLLOW_UP_MINUTES,
    MAX_MINUTES,
    AssignmentRejectForm,
    CriterionInput,
    CriterionRow,
    FollowUpForm,
    OrangeCheckForm,
    ReassignForm,
    SubmitScoreForm,
    criterion_rows,
    extract_criteria,
)
from orange_team.models import (
    MAX_POINTS,
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
    OrangeCheckIn,
    OrangeFollowUp,
    scores_total_problem,
)
from orange_team.services import (
    assign_all_checks,
    auto_assign_check,
    create_orange_score_from_assignment,
    rebalance_unstarted,
    update_check_criteria,
)
from team.models import Team


def _form_errors(form: forms.Form) -> str:
    """The form's errors as one message, each naming its field."""
    lines: list[str] = []
    for name, errors in form.errors.items():
        field = form.fields.get(name)
        prefix = f"{field.label or name.replace('_', ' ').capitalize()}: " if field is not None else ""
        lines += [f"{prefix}{error}" for error in errors]
    return " ".join(lines)


@require_permission("orange_team", "orange_team_lead", error_message="Only Orange Team members can access this page")
def dashboard(request: HttpRequest) -> HttpResponse:
    """Orange team dashboard showing check-in status and assignments."""
    user = cast(User, request.user)

    active_checkin = OrangeCheckIn.objects.filter(user=user, is_active=True).first()

    my_assignments = list(
        OrangeAssignment.objects.filter(user=user)
        .unfinished()
        .select_related("orange_check", "team")
        .prefetch_related("results__criterion")
        .order_by("orange_check__scheduled_at", "orange_check__title", "team__team_number")
    )
    for assignment in my_assignments:
        # Which scoring UI to show — criteria rubric vs a single score — from the
        # already-prefetched results, so this stays one query.
        assignment.has_criteria = len(assignment.results.all()) > 0  # type: ignore[attr-defined]

    followups = OrangeFollowUp.objects.filter(user=user, dismissed=False).select_related(
        "assignment__orange_check", "assignment__team"
    )

    is_lead = has_permission(user, "orange_team_lead")
    context: dict[str, object] = {
        "active_checkin": active_checkin,
        "assignments": my_assignments,
        "followups": followups,
        "is_lead": is_lead,
        "max_follow_up_minutes": MAX_FOLLOW_UP_MINUTES,
    }

    return render(request, "orange_team/dashboard.html", context)


@require_permission("orange_team", "orange_team_lead", error_message="Only Orange Team members can access this page")
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


@require_permission("orange_team_lead", error_message="Only leads can manage check-ins")
def admin_toggle_checkin(request: HttpRequest, user_id: int) -> HttpResponse:
    """Toggle check-in/out for another user (lead only)."""
    if request.method != "POST":
        return redirect("orange_team:check_list")
    target_user = get_object_or_404(User, pk=user_id)
    active = OrangeCheckIn.objects.filter(user=target_user, is_active=True).first()
    if active:
        active.is_active = False
        active.checked_out_at = timezone.now()
        active.save()
    else:
        OrangeCheckIn.objects.create(user=target_user)
    return redirect("orange_team:check_list")


@require_permission("orange_team_lead", error_message="Only leads can review assignments")
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


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def check_list(request: HttpRequest) -> HttpResponse:
    now = timezone.now()
    checks = list(
        OrangeCheck.objects.annotate(
            team_total=Count("assignments", distinct=True),
            scored_count=Count("assignments", filter=Q(assignments__status="approved"), distinct=True),
        )
        .select_related("created_by")
        .order_by("scheduled_at", "-created_at")
    )
    checkins = list(OrangeCheckIn.objects.filter(is_active=True).select_related("user").order_by("checked_in_at"))
    by_user: dict[int, list[OrangeAssignment]] = {}
    for assignment in (
        OrangeAssignment.objects.filter(user_id__in=[ci.user_id for ci in checkins])
        .unfinished()
        .select_related("orange_check", "team")
    ):
        by_user.setdefault(assignment.user_id, []).append(assignment)
    volunteers = []
    for checkin in checkins:
        mine = by_user.get(checkin.user_id, [])
        current = next((a for a in mine if a.orange_check.is_open(now)), None)
        upcoming = sorted(
            (a for a in mine if a.orange_check.is_upcoming(now)),
            key=lambda a: a.orange_check.scheduled_at or now,
        )
        volunteers.append(
            {
                "user": checkin.user,
                "checkin": checkin,
                "open_count": len(mine),
                "current_task": current,
                "next_task": upcoming[0] if upcoming else None,
            }
        )
    return render(
        request,
        "orange_team/check_list.html",
        {"checks": checks, "volunteers": volunteers, "is_lead": True},
    )


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def auto_assign_all(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("orange_team:check_list")
    now = timezone.now()
    # Every scheduled check that isn't closed yet — skips drafts (no schedule) and past windows.
    checks = [
        check
        for check in OrangeCheck.objects.filter(scheduled_at__isnull=False).order_by("scheduled_at", "created_at")
        if not check.is_closed(now)
    ]
    users = list(User.objects.filter(orange_checkins__is_active=True).distinct())
    teams = list(Team.objects.filter(is_active=True).order_by("team_number"))
    total = assign_all_checks(checks, users, teams)
    messages.success(request, f"Auto-assigned {total} team checks across {len(users)} volunteers.")
    return redirect("orange_team:check_list")


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def check_create(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _check_page(request, None, _EMPTY_CHECK, [])
    form, criteria = _posted_check(request, None)
    if form is None or criteria is None:
        return _check_page(request, None, _posted_values(request), criterion_rows(request.POST))

    user = cast(User, request.user)
    with transaction.atomic():
        orange_check = OrangeCheck.objects.create(**_check_fields(form), created_by=user)
        for c in criteria:
            OrangeCheckCriterion.objects.create(
                orange_check=orange_check, label=c["label"], points=c["points"], sort_order=c["sort_order"]
            )

    messages.success(request, f"Check '{orange_check.title}' created with {len(criteria)} criteria.")
    return redirect("orange_team:check_list")


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def check_detail(request: HttpRequest, check_id: int) -> HttpResponse:
    orange_check = get_object_or_404(
        OrangeCheck.objects.prefetch_related("criteria", "assignments__user", "assignments__team"),
        pk=check_id,
    )
    return render(
        request,
        "orange_team/check_detail.html",
        {
            "orange_check": orange_check,
            "is_lead": True,
        },
    )


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def check_edit(request: HttpRequest, check_id: int) -> HttpResponse:
    orange_check = get_object_or_404(OrangeCheck, pk=check_id)
    if request.method != "POST":
        shown = [CriterionRow(id=str(c.pk), label=c.label, points=str(c.points)) for c in orange_check.criteria.all()]
        return _check_page(request, orange_check, _check_values(orange_check), shown)
    form, criteria = _posted_check(request, orange_check)
    if form is None or criteria is None:
        # Show what was posted, rubric included: a page without the rows would save an empty rubric next time
        return _check_page(request, orange_check, _posted_values(request), criterion_rows(request.POST))

    with transaction.atomic():
        for name, value in _check_fields(form).items():
            setattr(orange_check, name, value)
        orange_check.save()
        update_check_criteria(orange_check, criteria)

    messages.success(request, f"Check '{orange_check.title}' updated.")
    return redirect("orange_team:check_detail", check_id=orange_check.pk)


_EMPTY_CHECK = {"title": "", "description": "", "scheduled_at": "", "time_limit_minutes": "", "max_points": "0"}


def _posted_check(
    request: HttpRequest, orange_check: OrangeCheck | None
) -> tuple[OrangeCheckForm | None, list[CriterionInput] | None]:
    """The check form and its rubric when both are valid; otherwise (None, None), with the problems as a message."""
    form = OrangeCheckForm(request.POST)
    problems = [] if form.is_valid() else [_form_errors(form)]
    try:
        criteria = extract_criteria(request.POST)
    except ValidationError as refused:
        problems += refused.messages
    else:
        worth = sum(c["points"] for c in criteria) if criteria else form.cleaned_data.get("max_points") or 0
        if not problems and (problem := scores_total_problem(orange_check.pk if orange_check else None, worth)):
            problems.append(problem)
    if problems:
        messages.error(request, " ".join(problems))
        return None, None
    return form, criteria


def _check_fields(form: OrangeCheckForm) -> dict[str, object]:
    minutes = form.cleaned_data.get("time_limit_minutes")
    return {
        "title": form.cleaned_data["title"],
        "description": form.cleaned_data["description"],
        "scheduled_at": form.cleaned_data["scheduled_at"] or None,
        "max_points": form.cleaned_data.get("max_points") or 0,
        "time_limit": timedelta(minutes=minutes) if minutes else None,
    }


def _check_values(check: OrangeCheck) -> dict[str, str]:
    """The check form's field values for a stored check."""
    scheduled = timezone.localtime(check.scheduled_at).strftime("%Y-%m-%dT%H:%M") if check.scheduled_at else ""
    minutes = str(int(check.time_limit.total_seconds() // 60)) if check.time_limit else ""
    return {
        "title": check.title,
        "description": check.description,
        "scheduled_at": scheduled,
        "time_limit_minutes": minutes,
        "max_points": str(check.max_points),
    }


def _posted_values(request: HttpRequest) -> dict[str, str]:
    """The check form's fields as posted, to show again with its errors."""
    form = OrangeCheckForm(request.POST)
    return {name: str(form[name].value() or "") for name in _EMPTY_CHECK}


def _check_page(
    request: HttpRequest, orange_check: OrangeCheck | None, values: dict[str, str], criteria: list[CriterionRow]
) -> HttpResponse:
    return render(
        request,
        "orange_team/check_form.html",
        {
            "mode": "create" if orange_check is None else "edit",
            "orange_check": orange_check,
            "values": values,
            "criteria": criteria,
            "max_points": MAX_POINTS,
            "max_minutes": MAX_MINUTES,
            "is_lead": True,
        },
    )


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
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


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def check_assign(request: HttpRequest, check_id: int) -> HttpResponse:
    """Show the assign grid for a check: every active team with its volunteer dropdown."""
    orange_check = get_object_or_404(OrangeCheck, pk=check_id)
    existing = {
        a.team_id: a for a in OrangeAssignment.objects.filter(orange_check=orange_check).select_related("user", "team")
    }
    active_teams = Team.objects.filter(is_active=True).order_by("team_number")
    rows = [{"team": team, "assignment": existing.get(team.id)} for team in active_teams]
    on_shift = list(User.objects.filter(orange_checkins__is_active=True).distinct().order_by("username"))
    scored = sum(1 for a in existing.values() if a.status == "approved")
    return render(
        request,
        "orange_team/assign.html",
        {
            "orange_check": orange_check,
            "rows": rows,
            "on_shift": on_shift,
            "scored": scored,
            "team_total": len(rows),
            "is_lead": True,
        },
    )


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def check_auto_assign(request: HttpRequest, check_id: int) -> HttpResponse:
    """Rotate on-shift volunteers across this check's teams (pending/missing cells only)."""
    if request.method != "POST":
        return redirect("orange_team:check_assign", check_id=check_id)
    orange_check = get_object_or_404(OrangeCheck, pk=check_id)
    users = list(User.objects.filter(orange_checkins__is_active=True).distinct())
    teams = list(Team.objects.filter(is_active=True).order_by("team_number"))
    count = auto_assign_check(orange_check, users, teams)
    messages.success(request, f"Auto-assigned {count} teams across {len(users)} volunteers.")
    return redirect("orange_team:check_assign", check_id=check_id)


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def check_rebalance(request: HttpRequest, check_id: int) -> HttpResponse:
    """Spread only the pending teams across whoever is still on shift."""
    if request.method != "POST":
        return redirect("orange_team:check_assign", check_id=check_id)
    orange_check = get_object_or_404(OrangeCheck, pk=check_id)
    users = list(User.objects.filter(orange_checkins__is_active=True).distinct())
    count = rebalance_unstarted(orange_check, users)
    messages.success(request, f"Rebalanced {count} unstarted teams.")
    return redirect("orange_team:check_assign", check_id=check_id)


@require_permission("orange_team_lead", error_message="Only leads can manage checks")
def reassign_team(request: HttpRequest, assignment_id: int) -> HttpResponse:
    """Reassign one team's volunteer; scored teams are locked."""
    if request.method != "POST":
        return redirect("orange_team:dashboard")
    assignment = get_object_or_404(OrangeAssignment.objects.select_related("orange_check", "team"), pk=assignment_id)
    check_id = assignment.orange_check_id
    if assignment.status in ("submitted", "approved"):
        messages.error(request, "Scored teams can't be reassigned.")
        return redirect("orange_team:check_assign", check_id=check_id)
    form = ReassignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Pick a volunteer who is checked in.")
        return redirect("orange_team:check_assign", check_id=check_id)
    new_user = User.objects.filter(pk=form.cleaned_data["user_id"], orange_checkins__is_active=True).first()
    if new_user is None:
        messages.error(request, "Pick a volunteer who is checked in.")
        return redirect("orange_team:check_assign", check_id=check_id)
    assignment.user = new_user
    assignment.save(update_fields=["user"])
    messages.success(request, f"Reassigned Team {assignment.team.team_number} to {new_user.username}.")
    return redirect("orange_team:check_assign", check_id=check_id)


def _criterion_result(body: bytes) -> tuple[int, bool] | None:
    """The criterion id and whether it was met, from the dashboard's JSON body; None for anything else."""
    try:
        data = json.loads(body)
    except ValueError:  # not JSON, or not UTF-8
        return None
    if not isinstance(data, dict):
        return None
    criterion_id, met = data.get("criterion_id"), data.get("met")
    if type(criterion_id) is not int or not isinstance(met, bool):
        return None
    return criterion_id, met


def assignment_save(request: HttpRequest, assignment_id: int) -> HttpResponse:
    """Autosave a criterion result for an assignment."""
    if not (has_permission(request.user, "orange_team") or has_permission(request.user, "orange_team_lead")):
        return JsonResponse({"error": "Access denied"}, status=403)
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    user = cast(User, request.user)
    assignment = OrangeAssignment.objects.filter(pk=assignment_id, user=user).first()
    if assignment is None:
        return JsonResponse({"error": "Assignment not found"}, status=404)

    if assignment.status in ("submitted", "approved"):
        return JsonResponse({"error": "Assignment already submitted"}, status=400)

    posted = _criterion_result(request.body)
    if posted is None:
        return JsonResponse({"error": "Invalid data"}, status=400)
    criterion_id, met = posted

    result = OrangeAssignmentResult.objects.filter(assignment=assignment, criterion_id=criterion_id).first()
    if result is None:
        return JsonResponse({"error": "Criterion not found"}, status=404)
    result.met = met
    result.save()

    if assignment.status == "pending":
        assignment.status = "in_progress"
        assignment.save()

    score = assignment.calculate_score()
    max_score = assignment.orange_check.max_score
    return JsonResponse({"score": score, "max_score": max_score})


@require_permission("orange_team", "orange_team_lead", error_message="Only Orange Team members can access this page")
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
    else:
        score_form = SubmitScoreForm(request.POST)
        if score_form.is_valid() and score_form.cleaned_data.get("score") is not None:
            posted = score_form.cleaned_data["score"]
            if posted > assignment.orange_check.max_score:
                messages.error(request, f"Score must be between 0 and {assignment.orange_check.max_score}.")
                return redirect("orange_team:dashboard")
            assignment.score = posted
        # else: keep any score already saved on the assignment
    assignment.status = "submitted"
    assignment.submitted_at = timezone.now()
    assignment.save()

    messages.success(
        request,
        f"Assignment submitted: {assignment.orange_check.title}"
        f" for Team {assignment.team.team_number}"
        f" ({assignment.score}/{assignment.orange_check.max_score})",
    )
    return redirect("orange_team:dashboard")


@require_permission("orange_team", "orange_team_lead", error_message="Only Orange Team members can access this page")
def followup_create(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return redirect("orange_team:dashboard")

    user = cast(User, request.user)
    form = FollowUpForm(request.POST)
    if not form.is_valid():
        messages.error(request, _form_errors(form))
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


@require_permission("orange_team", "orange_team_lead", error_message="Only Orange Team members can access this page")
def followup_dismiss(request: HttpRequest, followup_id: int) -> HttpResponse:
    if request.method != "POST":
        return redirect("orange_team:dashboard")

    user = cast(User, request.user)
    followup = get_object_or_404(OrangeFollowUp, pk=followup_id, user=user)
    followup.dismissed = True
    followup.save()
    return redirect("orange_team:dashboard")


@require_permission("orange_team_lead", error_message="Only leads can approve assignments")
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
        f"Approved: {assignment.orange_check.title} for Team {assignment.team.team_number} ({assignment.score} pts)",
    )
    return redirect("orange_team:review_queue")


@require_permission("orange_team_lead", error_message="Only leads can reject assignments")
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
        f"Rejected: {assignment.orange_check.title} for Team {assignment.team.team_number}",
    )
    return redirect("orange_team:review_queue")


@require_permission("orange_team_lead", error_message="Only leads can export scores")
def export_scores(request: HttpRequest) -> HttpResponse:
    assignments = (
        OrangeAssignment.objects.filter(status__in=["submitted", "approved"])
        .select_related("orange_check", "team", "user", "reviewed_by")
        .order_by("orange_check__title", "team__team_number")
    )

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="orange_checks.csv"'
    writer = csv_writer(response)
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
