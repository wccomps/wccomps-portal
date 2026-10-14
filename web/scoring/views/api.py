from django.http import HttpRequest, JsonResponse

from core.auth_utils import has_permission

from ..calculator import get_leaderboard


def _allowed(request: HttpRequest, *permissions: str) -> bool:
    return any(has_permission(request.user, p) for p in permissions)


def api_scores(request: HttpRequest) -> JsonResponse:
    if not _allowed(request, "gold_team", "white_team", "ticketing_admin"):
        return JsonResponse({"error": "Access denied"}, status=403)
    scores = get_leaderboard()
    data = [
        {
            "rank": score.rank,
            "team": f"Team {score.team.team_number}",
            "team_number": score.team.team_number,
            "total": float(score.total_score),
            "services": float(score.service_points),
            "injects": float(score.inject_points),
            "orange": float(score.orange_points),
            "red": float(score.red_deductions),
            "incidents": float(score.incident_recovery_points),
            "sla": float(score.sla_penalties),
        }
        for score in scores
    ]
    return JsonResponse({"scores": data})
