"""A team's scorecard frozen as JSON: its template context, Decimals kept exact.

The keys match what scoring/_scorecard_body.html and scoring/scorecard_print.html read, so both render a
snapshot unchanged. Decimals are stored as {"$d": "<str>"} because the templates compare and format them
as numbers (floatformat, c-score_value's add:0), which plain strings would break.
"""

from collections.abc import Iterable
from decimal import Decimal
from typing import cast

from .calculator import SCORE_COMPONENT_FIELDS, Standing
from .models import OrangeTeamScore, RedTeamScore

SNAPSHOT_VERSION = 1
_DECIMAL = "$d"


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return {_DECIMAL: str(value)}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_encode(item) for item in value]
    return value


def _decode(value: object) -> object:
    if isinstance(value, dict):
        if value.keys() == {_DECIMAL}:
            return Decimal(value[_DECIMAL])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def serialize_scorecard(context: dict[str, object]) -> dict[str, object]:
    """build_scorecard_context's result as JSON values, keeping only what the scorecard templates read."""
    score = cast(Standing, context["score"])
    plain = {
        "team": {"team_number": score.team.team_number},
        "score": {
            **{field: getattr(score, field) for field in (*SCORE_COMPONENT_FIELDS, "total_score")},
            "rank": score.rank,
        },
        "stats": context["stats"],
        "red_scores": [
            {
                "attack_type": {"name": finding.attack_type.name} if finding.attack_type else None,
                "affected_boxes_display": finding.affected_boxes_display,
                "affected_service": finding.affected_service,
                "outcomes_display": finding.outcomes_display,
                "points_per_team": finding.points_per_team,
            }
            for finding in cast(Iterable[RedTeamScore], context["red_scores"])
        ],
        "orange_scores": [
            {"description": check.description, "points_awarded": check.points_awarded}
            for check in cast(Iterable[OrangeTeamScore], context["orange_scores"])
        ],
        **{
            key: context[key]
            for key in ("red_total", "inject_total", "orange_total", "service_total", "service_sla_total", "scaling")
        },
    }
    return cast(dict[str, object], _encode(plain))


def context_from_snapshot(data: dict[str, object]) -> dict[str, object]:
    """A stored snapshot back as a template context."""
    return cast(dict[str, object], _decode(data))
