from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TypedDict

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q, QuerySet, Sum
from django.http import Http404

from team.models import Team

from .models import (
    Approvable,
    IncidentReport,
    InjectScore,
    OrangeTeamScore,
    RedTeamScore,
    ScoringExclusion,
    ScoringTemplate,
    ServiceScore,
)


class ScoreBreakdown(TypedDict):
    """Score breakdown returned by calculate_team_score()."""

    service_points: Decimal
    inject_points: Decimal
    orange_points: Decimal
    red_deductions: Decimal
    sla_penalties: Decimal
    point_adjustments: Decimal
    incident_recovery_points: Decimal
    total_score: Decimal


class DetailedScoreBreakdown(ScoreBreakdown):
    """Extended breakdown including raw scores, modifiers, and weights."""

    service_raw: Decimal
    inject_raw: Decimal
    orange_raw: Decimal
    service_modifier: Decimal
    inject_modifier: Decimal
    orange_modifier: Decimal
    service_weight: Decimal
    inject_weight: Decimal
    orange_weight: Decimal


SCORE_COMPONENT_FIELDS = (
    "service_points",
    "inject_points",
    "orange_points",
    "red_deductions",
    "sla_penalties",
    "point_adjustments",
    "incident_recovery_points",
)

RECOVERY_POINT_RATIO = Decimal("0.80")


def _get_modifiers(template: ScoringTemplate) -> tuple[Decimal, Decimal, Decimal]:
    """Derive each category's scaling modifier from its weight and raw maximum.

        total_pool = max(raw_max / (weight/100))
        modifier = (weight/100) × total_pool / raw_max

    The maxes are the single source of truth: editing one re-derives the scale.
    """
    hundred = Decimal("100")
    pairs = [
        (template.service_weight, template.service_max),
        (template.inject_weight, template.inject_max),
        (template.orange_weight, template.orange_max),
    ]
    # Largest raw_max / fractional_weight sets the common scale
    total_pool = max(raw_max / (weight / hundred) for weight, raw_max in pairs if weight > 0 and raw_max > 0)
    modifiers: list[Decimal] = []
    for weight, raw_max in pairs:
        if weight > 0 and raw_max > 0:
            modifiers.append(weight / hundred * total_pool / raw_max)
        else:
            modifiers.append(Decimal("0"))
    return modifiers[0], modifiers[1], modifiers[2]


class _RawScores(TypedDict):
    service: Decimal
    sla: Decimal
    adjustments: Decimal
    inject: Decimal
    orange: Decimal
    red: Decimal
    recovery: Decimal


def _raw_scores(teams: list[Team]) -> dict[int, _RawScores]:
    """Unscaled inputs per team: synced service scores plus approved submissions."""
    zero = Decimal("0")
    team_ids = [team.pk for team in teams]
    raw: dict[int, _RawScores] = {
        team_id: {
            "service": zero,
            "sla": zero,
            "adjustments": zero,
            "inject": zero,
            "orange": zero,
            "red": zero,
            "recovery": zero,
        }
        for team_id in team_ids
    }
    for service in ServiceScore.objects.filter(team_id__in=team_ids):
        raw[service.team_id]["service"] = service.service_points
        raw[service.team_id]["sla"] = service.sla_violations
        raw[service.team_id]["adjustments"] = service.point_adjustments

    def approved_totals(model: type[Approvable], team_field: str, points_field: str) -> dict[int, Decimal]:
        rows = (
            model._default_manager.filter(is_approved=True, **{f"{team_field}__in": team_ids})
            .values(team_field)
            .annotate(total=Sum(points_field))
        )
        return {row[team_field]: row["total"] for row in rows}

    for team_id, total in approved_totals(InjectScore, "team", "points_awarded").items():
        raw[team_id]["inject"] = total.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    for team_id, total in approved_totals(OrangeTeamScore, "team", "points_awarded").items():
        raw[team_id]["orange"] = total
    for team_id, total in approved_totals(RedTeamScore, "affected_teams", "points_per_team").items():
        raw[team_id]["red"] = zero - total
    for team_id, total in approved_totals(IncidentReport, "team", "points_returned").items():
        raw[team_id]["recovery"] = total
    return raw


def _breakdown(raw: _RawScores, template: ScoringTemplate) -> DetailedScoreBreakdown:
    service_mod, inject_mod, orange_mod = _get_modifiers(template)
    scaled_service = raw["service"] * service_mod
    scaled_inject = raw["inject"] * inject_mod
    scaled_orange = raw["orange"] * orange_mod

    two_places = Decimal("0.01")
    total_score = (
        scaled_service + scaled_inject + scaled_orange + raw["sla"] + raw["adjustments"] + raw["red"] + raw["recovery"]
    ).quantize(two_places, rounding=ROUND_HALF_UP)

    return {
        "service_points": scaled_service.quantize(two_places, rounding=ROUND_HALF_UP),
        "inject_points": scaled_inject.quantize(two_places, rounding=ROUND_HALF_UP),
        "orange_points": scaled_orange.quantize(two_places, rounding=ROUND_HALF_UP),
        "red_deductions": raw["red"],
        "sla_penalties": raw["sla"],
        "point_adjustments": raw["adjustments"],
        "incident_recovery_points": raw["recovery"],
        "total_score": total_score,
        "service_raw": raw["service"],
        "inject_raw": raw["inject"],
        "orange_raw": raw["orange"],
        "service_modifier": service_mod,
        "inject_modifier": inject_mod,
        "orange_modifier": orange_mod,
        "service_weight": template.service_weight,
        "inject_weight": template.inject_weight,
        "orange_weight": template.orange_weight,
    }


def _template() -> ScoringTemplate:
    return ScoringTemplate.objects.first() or ScoringTemplate()


def calculate_team_score(team: Team) -> ScoreBreakdown:
    """Calculate final score for a team using scaling-factor formula.

    Formula:
        total = (service × service_modifier)
              + (inject × inject_modifier)
              + (orange × orange_modifier)
              + sla_violations + point_adjustments
              + red_deductions + incident_recovery
    """
    detailed = calculate_team_score_detailed(team)
    return {
        "service_points": detailed["service_points"],
        "inject_points": detailed["inject_points"],
        "orange_points": detailed["orange_points"],
        "red_deductions": detailed["red_deductions"],
        "sla_penalties": detailed["sla_penalties"],
        "point_adjustments": detailed["point_adjustments"],
        "incident_recovery_points": detailed["incident_recovery_points"],
        "total_score": detailed["total_score"],
    }


def calculate_team_score_detailed(team: Team) -> DetailedScoreBreakdown:
    """Like calculate_team_score but also returns raw scores, modifiers, and weights."""
    return _breakdown(_raw_scores([team])[team.pk], _template())


@dataclass
class Standing:
    """A team's current score, computed from the scoring inputs on every read."""

    team: Team
    service_points: Decimal
    inject_points: Decimal
    orange_points: Decimal
    red_deductions: Decimal
    sla_penalties: Decimal
    point_adjustments: Decimal
    incident_recovery_points: Decimal
    total_score: Decimal
    is_excluded: bool
    rank: int | None = None

    @property
    def has_scoring_activity(self) -> bool:
        return any(getattr(self, field) != 0 for field in SCORE_COMPONENT_FIELDS)

    @property
    def school_name(self) -> str:
        try:
            return self.team.school_info.school_name
        except ObjectDoesNotExist:
            return ""

    @property
    def school_emails(self) -> list[str]:
        return self.team.school_emails


def compute_standings() -> list[Standing]:
    """Every active team's score, highest first.

    Teams with scoring activity that are not excluded are ranked 1..n; the rest have rank None.
    """
    teams = list(Team.objects.filter(is_active=True).select_related("school_info").order_by("team_number"))
    raw = _raw_scores(teams)
    template = _template()
    excluded_team_ids = set(ScoringExclusion.objects.values_list("team_id", flat=True))

    standings = []
    for team in teams:
        breakdown = _breakdown(raw[team.pk], template)
        standings.append(
            Standing(
                team=team,
                is_excluded=team.pk in excluded_team_ids,
                total_score=breakdown["total_score"],
                **{field: breakdown[field] for field in SCORE_COMPONENT_FIELDS},  # type: ignore[literal-required]
            )
        )
    standings.sort(key=lambda standing: standing.total_score, reverse=True)

    rank = 0
    for standing in standings:
        if standing.has_scoring_activity and not standing.is_excluded:
            rank += 1
            standing.rank = rank
    return standings


def get_leaderboard(standings: list[Standing] | None = None) -> list[Standing]:
    """Ranked standings in rank order: excluded teams and teams with no scoring activity left out."""
    if standings is None:
        standings = compute_standings()
    return [standing for standing in standings if standing.rank is not None]


def get_standing(team_number: int, standings: list[Standing]) -> Standing:
    """The standing of an active team, or 404."""
    for standing in standings:
        if standing.team.team_number == team_number:
            return standing
    raise Http404(f"No active team {team_number}")


def suggest_red_score_matches(incident: IncidentReport) -> QuerySet[RedTeamScore]:
    """Red team scores against the incident's team that share its source IP (or an IP pool
    containing it), one of its boxes, or its service. Newest first, at most 10.
    """
    from .models import RedTeamIPPool

    query = Q(affected_teams=incident.team)

    filters = Q()
    if incident.source_ip:
        filters |= Q(source_ip=incident.source_ip)
        pool_ids = [pool.id for pool in RedTeamIPPool.objects.all() if pool.contains_ip(str(incident.source_ip))]
        if pool_ids:
            filters |= Q(source_ip_pool_id__in=pool_ids)
    if incident.affected_boxes:
        for box in incident.affected_boxes:
            filters |= Q(affected_boxes__contains=[box])
    if incident.affected_service:
        filters |= Q(affected_service=incident.affected_service)

    if filters:
        query &= filters

    scores = RedTeamScore.objects.filter(query).distinct().order_by("-created_at")

    return scores[:10]


def calculate_suggested_recovery_points(incident: IncidentReport, red_score: RedTeamScore) -> Decimal:
    """Suggested recovery points: RECOVERY_POINT_RATIO of the red team deduction, as a positive value."""
    deduction_amount = abs(red_score.points_per_team)
    suggested_return = deduction_amount * RECOVERY_POINT_RATIO
    return suggested_return
