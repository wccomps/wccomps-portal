from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from core.admin_views.places import SetupSection, setup_url
from core.auth_utils import has_permission, require_permission

from ..calculator import compute_standings, get_leaderboard, get_standing
from ..scorecard import build_scorecard_context
from .export import render_scorecard_pdf


@require_permission(
    "gold_team",
    "white_team",
    "red_team",
    "ticketing_admin",
    "orange_team",
    "ticketing_support",
)
def leaderboard(request: HttpRequest) -> HttpResponse:
    """Restricted leaderboard view."""
    is_admin = has_permission(request.user, "admin")
    show_schools = is_admin and (
        request.GET.get("schools", "").lower() in ("1", "true", "yes")
        or request.GET.get("names", "").lower() in ("school", "schools")
    )
    return render(
        request,
        "scoring/leaderboard.html",
        {
            "scores": get_leaderboard(),
            "show_schools": show_schools,
            "is_admin": is_admin,
            "scoring_engine_url": setup_url(SetupSection.SCORING_ENGINE),
        },
    )


def _scorecard_context(team_number: int) -> dict[str, object]:
    standings = compute_standings()
    return build_scorecard_context(get_standing(team_number, standings), standings)


@require_permission(
    "gold_team",
    "white_team",
    "red_team",
    "ticketing_admin",
    "orange_team",
    "ticketing_support",
    error_message="Only authorized staff can view scorecards",
)
def scorecard(request: HttpRequest, team_number: int) -> HttpResponse:
    """Detailed scorecard for a single team."""
    from django.urls import reverse

    context = _scorecard_context(team_number)
    context["pdf_url"] = reverse("leaderboard_scorecard_pdf", args=[team_number])
    if has_permission(request.user, "gold_team") or has_permission(request.user, "admin"):
        context["email_url"] = reverse("scoring:email_scorecard", args=[team_number])
    return render(request, "scoring/scorecard.html", context)


@require_permission(
    "gold_team",
    "white_team",
    "red_team",
    "ticketing_admin",
    "orange_team",
    "ticketing_support",
    error_message="Only authorized staff can export scorecards",
)
def scorecard_pdf(request: HttpRequest, team_number: int) -> HttpResponse:
    context = _scorecard_context(team_number)
    pdf_bytes = render_scorecard_pdf(context, request=request)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="team-{team_number:02d}-scorecard.pdf"'
    return response
