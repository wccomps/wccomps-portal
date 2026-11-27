"""A team's scorecard frozen as JSON: its template context, Decimals kept exact.

The keys match what scoring/_scorecard_body.html and scoring/scorecard_print.html read, so both render a
snapshot unchanged. Decimals are stored as {"$d": "<str>"} and decoded back, so the templates get the same
Decimals a live scorecard gives them.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import cast

from .calculator import SCORE_COMPONENT_FIELDS, Standing
from .models import OrangeTeamScore, RedTeamScore

SNAPSHOT_VERSION = 1

# Stored v1 contract, independent of the live calculator and current writer version.
_V1_CATEGORIES = {
    "service_points": "services",
    "inject_points": "injects",
    "orange_points": "orange",
    "red_deductions": "red",
    "sla_penalties": "sla",
    "incident_recovery_points": "recovery",
    "point_adjustments": "adjustments",
}
_V1_SCALED = ("service", "inject", "orange")
_DECIMAL = "$d"


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return {_DECIMAL: str(value)}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
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
    cohort: int | None = None

    @classmethod
    def from_standing(cls, standing: Standing) -> FinalScore:
        return cls(
            standing.team.team_number, standing.team.team_name, standing.rank, standing.total_score,
            {key: getattr(standing, key) for key in SCORE_COMPONENT_FIELDS},
            sum(line["service_credit"] for line in standing.service_corrections),
            sum(line["sla_credit"] for line in standing.service_corrections),
        )


@dataclass(frozen=True)
class SchoolSnapshot:
    score: FinalScore
    scorecard: dict[str, object]


def scorecard_fingerprint(schools: dict[int, SchoolSnapshot]) -> str:
    """Preserve the existing mail-audit encoding of school → (slot, serialized card)."""
    payload = {
        str(school): (card.score.team_number, card.scorecard)
        for school, card in schools.items()
    }
    normalized = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


def validate_frozen_ranks(scores: Iterable[FinalScore]) -> None:
    """V1 ranked cards descend by total; a saved school subset may have gaps or tied totals."""
    frozen = list(scores)
    if len({score.cohort for score in frozen}) > 1:
        raise ValueError("Saved result cohorts conflict. Nothing was closed out.")
    ranked = sorted((score.rank, score.total) for score in frozen if score.rank is not None)
    for (rank, total), (next_rank, next_total) in pairwise(ranked):
        if rank == next_rank or total < next_total:
            raise ValueError("Saved result ranks conflict. Nothing was closed out.")


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError
    return cast(list[dict[str, object]], value)


def _fields(row: dict[str, object], *, numbers: str = "", text: str = "", flags: str = "") -> None:
    for key in numbers.split():
        _snapshot_number(row[key])
    if any(not isinstance(row[key], str) for key in text.split()):
        raise ValueError
    if any(type(row[key]) is not bool for key in flags.split()):
        raise ValueError


def _validate_v1_details(context: dict[str, object], components: dict[str, Decimal], rank: int | None) -> int:
    stats, scaling = context["stats"], context["scaling"]
    if not isinstance(stats, dict) or not isinstance(scaling, dict):
        raise ValueError
    count = stats["team_count"]
    if type(count) is not int or count < 0:
        raise ValueError
    _snapshot_number(stats["total_delta"])
    weights = [_snapshot_number(scaling[f"{name}_weight"]) for name in _V1_SCALED]
    if any(not 0 <= weight <= 100 for weight in weights) or sum(weights) != 100:
        raise ValueError
    for name in _V1_SCALED:
        _snapshot_number(scaling[f"{name}_raw"])
        if _snapshot_number(scaling[f"{name}_modifier"]) < 0:
            raise ValueError
    if not isinstance(stats["insights"], list) or not all(isinstance(item, str) for item in stats["insights"]):
        raise ValueError
    categories = stats["category_ranks"]
    if not isinstance(categories, dict):
        raise ValueError
    if rank is not None and any(components[field] and name not in categories for field, name in _V1_CATEGORIES.items()):
        raise ValueError
    for row in _rows(list(categories.values())):
        _positive_rank(row["rank"])
        if rank is not None and cast(int, row["rank"]) > count:
            raise ValueError
        _fields(row, numbers="avg min max value delta", flags="below_avg")
    for row in _rows(stats["service_stats"]):
        _positive_rank(row["rank"])
        _fields(row, numbers="points avg delta sla_violations", text="name", flags="below_avg")
    for row in _rows(stats["inject_stats"]):
        _positive_rank(row["rank"])
        _fields(row, numbers="points avg max delta", text="name feedback", flags="below_avg")
        if row.get("max_points") is not None:
            _snapshot_number(row["max_points"])
    for row in _rows(stats["neighbors"]):
        _positive_rank(row["rank"])
        neighbor = cast(int, row["rank"])
        if rank is None or neighbor > count or abs(neighbor - rank) > 1:
            raise ValueError
        _fields(row, numbers="total_score gap")
    for row in _rows(context["red_scores"]):
        _fields(row, numbers="points_per_team", text="affected_boxes_display affected_service")
        attack = row["attack_type"]
        if attack is not None and (not isinstance(attack, dict) or not isinstance(attack.get("name"), str)):
            raise ValueError
        if not isinstance(row["outcomes_display"], list) or not all(
            isinstance(item, str) for item in row["outcomes_display"]
        ):
            raise ValueError
    for row in _rows(context["orange_scores"]):
        _fields(row, numbers="points_awarded", text="description")
        if row.get("max_points") is not None:
            _snapshot_number(row["max_points"])
    for total, rows in (
        ("red_total", context["red_scores"]),
        ("orange_total", context["orange_scores"]),
        ("inject_total", stats["inject_stats"]),
        ("service_total", stats["service_stats"]),
        ("service_sla_total", stats["service_stats"]),
    ):
        if _snapshot_number(context[total]) and not rows:
            raise ValueError
    return count


def _positive_rank(value: object) -> None:
    if type(value) is not int or value < 1:
        raise ValueError


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
    """Read the stored version without reconstructing a live standing."""
    if version != 1:
        raise ValueError("A saved scorecard uses an unsupported version. Nothing was closed out.")
    try:
        context = context_from_snapshot(data)
        score, team = context["score"], context["team"]
        if (
            not isinstance(score, dict) or not isinstance(team, dict)
            or type(team["team_number"]) is not int or team["team_number"] != number
            or score["rank"] != rank or (rank is not None and type(score["rank"]) is not int)
            or _snapshot_number(score["total_score"]) != total
        ):
            raise ValueError
        components = {field: _snapshot_number(score[field]) for field in _V1_CATEGORIES}
        count = _validate_v1_details(context, components, rank)
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
            modifier = _snapshot_number(line["service_modifier"])
            if modifier < 0 or type(raw) is not int or type(sla) is not int or sla < 0:
                raise ValueError
            raw_credit += raw
            sla_credit += sla
    except (KeyError, TypeError, ValueError, InvalidOperation, RecursionError):
        raise ValueError("A saved scorecard cannot be read. Nothing was closed out.") from None
    if rank is not None and not 1 <= rank <= count:
        raise ValueError("Saved result rank is outside its recorded cohort. Nothing was closed out.")
    return FinalScore(number, name, rank, total, components, raw_credit, sla_credit, count)
