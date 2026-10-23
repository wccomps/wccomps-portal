from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from core.auth_utils import has_permission, require_permission
from core.utils import by_approval, by_team, chosen_team, list_filters, requested_sort, stable_order, team_numbers
from team.models import Team

from ..forms import ApproveInjectFeedbackForm, SaveInjectFeedbackForm
from ..maxes import inject_max_from_titles, set_template_max
from ..models import InjectScore

MIN_GRADES_FOR_OUTLIER = 3
OUTLIER_Z_SCORE_THRESHOLD = 1.5


@require_permission("white_team", "gold_team", error_message="Only White/Gold Team members can access inject grading")
def inject_grading(request: HttpRequest) -> HttpResponse:
    """Inject grading interface - select inject, grade all teams."""
    from quotient.client import QuotientClient

    client = QuotientClient()
    injects = client.get_injects()

    if injects is None:
        return render(request, "scoring/inject_grading.html", {"quotient_available": False, "no_injects": False})

    if len(injects) == 0:
        return render(request, "scoring/inject_grading.html", {"quotient_available": True, "no_injects": True})

    inject_choices = [(str(i.inject_id), i.title) for i in injects]
    inject_lookup = {str(i.inject_id): i for i in injects}

    selected_inject_id = request.GET.get("inject")

    teams = Team.objects.filter(is_active=True).order_by("team_number")

    if request.method == "POST":
        from ..forms import InjectGradingForm

        team_numbers = [t.team_number for t in teams]
        grading_form = InjectGradingForm(request.POST, team_numbers=team_numbers)
        if grading_form.is_valid():
            selected_inject_id = selected_inject_id or grading_form.cleaned_data["inject_id"]
            selected_inject = inject_lookup.get(selected_inject_id) if selected_inject_id else None

            if selected_inject:
                with transaction.atomic():
                    grades_saved = 0
                    user = cast(User, request.user)
                    current = {
                        g.team_id: g
                        for g in InjectScore.objects.select_for_update().filter(inject_id=selected_inject_id)
                    }

                    # Feedback is approved as it is saved only when the white team lead enters it.
                    is_lead = has_permission(user, "white_team_lead")
                    inject_max_points = selected_inject.max_points  # same for every team this inject
                    for team in teams:
                        num = team.team_number
                        points_value = grading_form.cleaned_data.get(f"points_team_{num}")
                        feedback_value = grading_form.cleaned_data.get(f"feedback_team_{num}") or ""
                        feedback_value = feedback_value.replace("\r\n", "\n").strip()
                        grade = current.get(team.id)

                        # The form posts every team's points as the page loaded them; only write a value this grader
                        # changed (vs the loaded one) and that differs from what is stored, so a stale page can't revert
                        # a grade saved since.
                        points_edited = (
                            points_value is not None
                            and not (
                                f"loaded_team_{num}" in request.POST
                                and points_value == grading_form.cleaned_data.get(f"loaded_team_{num}")
                            )
                            and (grade is None or grade.points_awarded != points_value)
                        )
                        stored_feedback = (grade.feedback if grade else "").replace("\r\n", "\n").strip()
                        feedback_edited = feedback_value != stored_feedback

                        if not points_edited and not feedback_edited:
                            continue
                        if grade is None:
                            if points_value is None:
                                # No grade and no points: nothing to attach feedback to
                                continue
                            # get_or_create: another grader may be saving this team's first grade right now
                            grade, _ = InjectScore.objects.select_for_update().get_or_create(
                                team=team,
                                inject_id=selected_inject_id,
                                defaults={
                                    "inject_name": selected_inject.title,
                                    "points_awarded": points_value,
                                    "graded_by": user,
                                },
                            )

                        grade.inject_name = selected_inject.title
                        grade.max_points = inject_max_points
                        if points_edited and points_value is not None:  # the None check is for the type checker
                            grade.points_awarded = points_value
                            grade.graded_by = user
                            grade.graded_at = timezone.now()
                            # An approval was of the old points; the new ones go back for review
                            grade.is_approved = False
                            grade.approved_by = None
                            grade.approved_at = None
                        if feedback_edited:
                            grade.feedback = feedback_value
                            grade.feedback_approved = is_lead
                            grade.feedback_approved_by = user if is_lead else None
                        grade.save()
                        grades_saved += 1

                    if grades_saved:
                        messages.success(request, f"Saved {grades_saved} grades for {selected_inject.title}")
                        # Keep the reactive inject scaling current from the inject point values. After
                        # commit, so a failure deriving the max can never roll back the saved grades.
                        transaction.on_commit(lambda: set_template_max("inject_max", inject_max_from_titles(injects)))
            return redirect(f"{reverse('scoring:inject_grading')}?inject={selected_inject_id}")
        errors = "; ".join(f"{field.removeprefix('points_team_')}: {e[0]}" for field, e in grading_form.errors.items())
        messages.error(request, f"Nothing saved. Fix these teams' points: {errors}")

    selected_inject = inject_lookup.get(selected_inject_id) if selected_inject_id else None

    team_data = []
    if selected_inject:
        existing = InjectScore.objects.filter(inject_id=selected_inject_id).select_related("team", "graded_by")
        grade_by_team = {g.team_id: g for g in existing}

        for team in teams:
            grade = grade_by_team.get(team.id)
            team_data.append(
                {
                    "team": team,
                    "grade": grade,
                    "points": grade.points_awarded if grade else None,
                    "feedback": grade.feedback if grade else "",
                }
            )

    context = {
        "quotient_available": True,
        "inject_choices": inject_choices,
        "selected_inject": selected_inject,
        "selected_inject_id": selected_inject_id,
        "team_data": team_data,
    }

    if request.headers.get("HX-Request"):
        return render(request, "scoring/partials/inject_grading_content.html", context)

    return render(request, "scoring/inject_grading.html", context)


@require_permission("white_team", error_message="Only White Team or Gold Team members can review inject grades")
def inject_grades_review(request: HttpRequest) -> HttpResponse:
    """Review and approve inject grades."""
    import statistics
    from collections import defaultdict

    from django.core.paginator import Paginator

    filters, filtered = list_filters(
        request, {"status": "pending", "inject": "", "team": "", "outliers": "", "search": ""}
    )
    # The inject grades table's sortable columns
    sort_by = requested_sort(
        request, ("inject_name", "team__team_number", "points_awarded", "graded_at"), "inject_name"
    )

    base_query = InjectScore.objects.select_related("team", "graded_by")

    team = chosen_team(filters["team"])
    base_query = by_team(by_approval(base_query, filters["status"]), team)

    if filters["inject"]:
        base_query = base_query.filter(inject_id=filters["inject"])

    if filters["search"]:
        from django.db.models import Q

        base_query = base_query.filter(
            Q(inject_name__icontains=filters["search"]) | Q(inject_id__icontains=filters["search"])
        )

    base_query = stable_order(base_query, sort_by)

    # Outliers are computed among the grades that match the current filters
    # Dynamically add is_outlier and std_devs_from_mean attrs to grade objects
    all_grades_for_outlier_calc = list(base_query)
    inject_grades_map: dict[str, list[InjectScore]] = defaultdict(list)
    for grade in all_grades_for_outlier_calc:
        inject_grades_map[grade.inject_id].append(grade)

    for grades in inject_grades_map.values():
        if len(grades) >= MIN_GRADES_FOR_OUTLIER:
            points_list = [float(g.points_awarded) for g in grades]
            mean = statistics.mean(points_list)
            try:
                std_dev = statistics.stdev(points_list)
                for grade in grades:
                    points = float(grade.points_awarded)
                    z_score = abs(points - mean) / std_dev if std_dev > 0 else 0
                    grade.is_outlier = z_score > OUTLIER_Z_SCORE_THRESHOLD
                    grade.std_devs_from_mean = z_score
            except statistics.StatisticsError:
                for grade in grades:
                    grade.is_outlier = False
                    grade.std_devs_from_mean = 0
        else:
            for grade in grades:
                grade.is_outlier = False
                grade.std_devs_from_mean = 0

    if filters["outliers"] == "1":
        all_grades_for_outlier_calc = [g for g in all_grades_for_outlier_calc if g.is_outlier]

    page_obj = Paginator(all_grades_for_outlier_calc, 50).get_page(request.GET.get("page"))

    available_injects = InjectScore.objects.values("inject_id", "inject_name").distinct().order_by("inject_name")

    context = {
        "page_obj": page_obj,
        "has_unapproved": InjectScore.objects.filter(is_approved=False).exists(),
        "available_injects": available_injects,
        "team_numbers": team_numbers(Team.objects.filter(inject_grades__isnull=False), team),
        "chosen_team": team,
        "filters": filters,
        "filtered": filtered,
        "sort_by": sort_by,
    }

    if request.headers.get("HX-Request"):
        return render(request, "scoring/partials/inject_grades_table.html", context)

    return render(request, "scoring/review_inject_grades.html", context)


@require_permission("white_team", error_message="Only White Team or Gold Team members can approve inject grades")
@transaction.atomic
@require_http_methods(["POST"])
def inject_grades_bulk_approve(request: HttpRequest) -> HttpResponse:
    """Bulk approve inject grades."""
    from core.utils import bulk_approve

    user = cast(User, request.user)
    return bulk_approve(
        request,
        field_name="grade_ids",
        queryset=InjectScore.objects.filter(is_approved=False),
        redirect_url="scoring:inject_grades_review",
        item_label="inject grade",
        on_item=lambda item: item.approve(user),
    )


@require_permission("gold_team", "white_team", error_message="Only Gold/White Team members can review inject feedback")
def review_inject_feedback(request: HttpRequest) -> HttpResponse:
    """Gold/White team review of inject feedback before showing to teams."""
    filters, filtered = list_filters(request, {"inject": "", "status": "all"})

    scores = (
        InjectScore.objects.filter(is_approved=True)
        .exclude(feedback="")
        .select_related("team", "feedback_approved_by")
        .order_by("inject_name", "team__team_number")
    )

    if filters["inject"]:
        scores = scores.filter(inject_id=filters["inject"])
    scores = by_approval(scores, filters["status"], "feedback_approved")

    inject_choices = (
        InjectScore.objects.filter(is_approved=True)
        .exclude(feedback="")
        .values_list("inject_id", "inject_name")
        .distinct()
        .order_by("inject_id")
    )

    has_unapproved = scores.filter(feedback_approved=False).exists()

    context = {
        "scores": scores,
        "inject_choices": inject_choices,
        "filters": filters,
        "filtered": filtered,
        "has_unapproved": has_unapproved,
    }

    if request.headers.get("HX-Request"):
        return render(request, "scoring/partials/inject_feedback_table.html", context)

    return render(request, "scoring/review_inject_feedback.html", context)


@require_permission("gold_team", "white_team", error_message="Only Gold/White Team members can edit inject feedback")
@transaction.atomic
@require_http_methods(["POST"])
def save_inject_feedback(request: HttpRequest) -> HttpResponse:
    form = SaveInjectFeedbackForm(request.POST)
    if not form.is_valid():
        messages.warning(request, "No score specified")
        return redirect("scoring:review_inject_feedback")

    score_id = form.cleaned_data["score_id"]
    feedback_text = form.cleaned_data.get("feedback", "")

    score = get_object_or_404(InjectScore.objects.select_for_update().select_related("team"), pk=score_id)
    if score.feedback != feedback_text:
        user = cast(User, request.user)
        is_lead = has_permission(user, "white_team_lead")
        score.feedback = feedback_text
        score.feedback_approved = is_lead
        score.feedback_approved_by = user if is_lead else None
        score.save(update_fields=["feedback", "feedback_approved", "feedback_approved_by"])

    messages.success(request, f"Saved feedback for {score.inject_name}, Team {score.team.team_number}")
    return redirect("scoring:review_inject_feedback")


@require_permission(
    "gold_team", "white_team_lead", error_message="Only Gold Team or White Team Leads can approve inject feedback"
)
@transaction.atomic
@require_http_methods(["POST"])
def approve_inject_feedback(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)

    form = ApproveInjectFeedbackForm(request.POST)
    if not form.is_valid():
        messages.warning(request, "No score specified")
        return redirect("scoring:review_inject_feedback")

    score_id = form.cleaned_data["score_id"]
    score = get_object_or_404(InjectScore.objects.select_for_update().select_related("team"), pk=score_id)
    score.feedback_approved = True
    score.feedback_approved_by = user
    score.save(update_fields=["feedback_approved", "feedback_approved_by"])

    messages.success(request, f"Approved feedback for {score.inject_name}, Team {score.team.team_number}")
    return redirect("scoring:review_inject_feedback")


@require_permission(
    "gold_team", "white_team_lead", error_message="Only Gold Team or White Team Leads can approve inject feedback"
)
@transaction.atomic
@require_http_methods(["POST"])
def bulk_approve_inject_feedback(request: HttpRequest) -> HttpResponse:
    from core.utils import bulk_approve

    user = cast(User, request.user)

    def approve(score: InjectScore) -> None:
        score.feedback_approved = True
        score.feedback_approved_by = user
        score.save(update_fields=["feedback_approved", "feedback_approved_by"])

    return bulk_approve(
        request,
        field_name="score_ids",
        queryset=InjectScore.objects.filter(feedback_approved=False),
        redirect_url="scoring:review_inject_feedback",
        item_label="feedback item",
        on_item=approve,
    )
