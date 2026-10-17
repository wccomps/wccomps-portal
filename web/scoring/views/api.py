from django.http import HttpRequest, JsonResponse

from core.auth_utils import require_permission
from core.errors import json_endpoint

from ..calculator import get_leaderboard


@json_endpoint
@require_permission("gold_team", "white_team", "ticketing_admin")
def api_scores(request: HttpRequest) -> JsonResponse:
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
