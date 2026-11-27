"""A team's scorecard frozen as JSON: its template context, Decimals kept exact.

The keys match what scoring/_scorecard_body.html and scoring/scorecard_print.html read, so both render a
snapshot unchanged. Decimals are stored as {"$d": "<str>"} and decoded back, so the templates get the same
Decimals a live scorecard gives them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
            **({"service_corrections": score.service_corrections} if score.service_corrections else {}),
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
            {
                "description": check.description,
                "points_awarded": check.points_awarded,
                "max_points": check.max_points,
            }
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


@dataclass(frozen=True)
class FinalScore:
    team_number: int
    team_name: str
    rank: int | None
    total: Decimal
    components: dict[str, Decimal]
    service_credit: int
    sla_credit: int

    @classmethod
    def from_standing(cls, standing: Standing) -> FinalScore:
        return cls(
            standing.team.team_number, standing.team.team_name, standing.rank, standing.total_score,
            {key: getattr(standing, key) for key in SCORE_COMPONENT_FIELDS},
            sum(line["service_credit"] for line in standing.service_corrections),
            sum(line["sla_credit"] for line in standing.service_corrections),
        )


def _snapshot_number(value: object) -> Decimal:
    if type(value) not in (Decimal, int, str):
        raise ValueError
    number = Decimal(cast(Decimal | int | str, value))
    if not number.is_finite():
        raise ValueError
    return number


def frozen_final_score(
    data: dict[str, object], version: int, number: int, name: str, rank: int | None, total: Decimal,
) -> FinalScore:
    """Validate the frozen v1 export inputs without rewriting the stored card or creating live scoring state."""
    try:
        context = context_from_snapshot(data)
        score, team = context["score"], context["team"]
        if (
            version != SNAPSHOT_VERSION or not isinstance(score, dict) or not isinstance(team, dict)
            or type(team["team_number"]) is not int or team["team_number"] != number
            or score["rank"] != rank or (rank is not None and type(score["rank"]) is not int)
            or _snapshot_number(score["total_score"]) != total
            or not isinstance(context["stats"], dict) or not isinstance(context["scaling"], dict)
            or not isinstance(context["red_scores"], list) or not isinstance(context["orange_scores"], list)
        ):
            raise ValueError
        components = {field: _snapshot_number(score[field]) for field in SCORE_COMPONENT_FIELDS}
        for field in ("red_total", "inject_total", "orange_total", "service_total", "service_sla_total"):
            _snapshot_number(context[field])
        corrections = score.get("service_corrections", [])
        if not isinstance(corrections, list):
            raise ValueError
        raw_credit = sla_credit = 0
        for line in corrections:
            if not isinstance(line, dict) or any(
                not isinstance(line[key], str) or not line[key] for key in ("service_name", "ruling")
            ):
                raise ValueError
            raw, sla = line["service_credit"], line["sla_credit"]
            _snapshot_number(line["service_modifier"])
            if type(raw) is not int or type(sla) is not int or sla < 0:
                raise ValueError
            raw_credit += raw
            sla_credit += sla
    except (KeyError, TypeError, ValueError, InvalidOperation, RecursionError):
        raise ValueError("A saved scorecard cannot be read. Nothing was closed out.") from None
    return FinalScore(number, name, rank, total, components, raw_credit, sla_credit)
