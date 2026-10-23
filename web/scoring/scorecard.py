"""A team's scorecard context, shared by the scorecard views, the PDFs and Close Out's frozen results.

Kept apart from the views so Close Out (and anything importing it at startup) doesn't load WeasyPrint.
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

from .calculator import SCALING_FIELDS, Standing, calculate_team_score_detailed, get_leaderboard
from .models import InjectScore, OrangeTeamScore, RedTeamScore, ServiceDetail


@dataclass(frozen=True)
class ScorecardPeerContext:
    """Pre-fetched inject and service rows across all teams in a standings cohort."""

    ranked_team_ids: set[int]
    peer_inject_points: dict[str, list[Decimal]]
    peer_service_points: dict[str, list[Decimal]]
    injects_by_team: dict[int, list[InjectScore]]
    services_by_team: dict[int, list[ServiceDetail]]


def build_peer_context(standings: list[Standing], extra_team_ids: set[int] | None = None) -> ScorecardPeerContext:
    """Fetch approved injects and service details once for a cohort of standings."""
    ranked = get_leaderboard(standings)
    ranked_team_ids = {s.team.pk for s in ranked}
    query_team_ids = {s.team.pk for s in standings} | (extra_team_ids or set())

    all_injects = list(
        InjectScore.objects.filter(team_id__in=query_team_ids, is_approved=True).order_by("inject_name", "pk")
    )
    peer_inject_points: dict[str, list[Decimal]] = defaultdict(list)
    injects_by_team: dict[int, list[InjectScore]] = defaultdict(list)
    for row in all_injects:
        injects_by_team[row.team_id].append(row)
        if row.team_id in ranked_team_ids:
            peer_inject_points[row.inject_id].append(row.points_awarded)

    all_services = list(ServiceDetail.objects.filter(team_id__in=query_team_ids).order_by("service_name", "pk"))
    peer_service_points: dict[str, list[Decimal]] = defaultdict(list)
    services_by_team: dict[int, list[ServiceDetail]] = defaultdict(list)
    for svc_row in all_services:
        services_by_team[svc_row.team_id].append(svc_row)
        if svc_row.team_id in ranked_team_ids:
            peer_service_points[svc_row.service_name].append(svc_row.points)

    return ScorecardPeerContext(
        ranked_team_ids=ranked_team_ids,
        peer_inject_points=peer_inject_points,
        peer_service_points=peer_service_points,
        injects_by_team=injects_by_team,
        services_by_team=services_by_team,
    )


class _CategoryRank(TypedDict):
    rank: int
    avg: Decimal
    min: Decimal
    max: Decimal
    value: Decimal
    delta: int
    below_avg: bool


class _InjectStat(TypedDict):
    name: str
    points: Decimal
    rank: int
    avg: Decimal
    max: Decimal
    delta: int
    below_avg: bool
    feedback: str


class _ServiceStat(TypedDict):
    name: str
    points: Decimal
    rank: int
    avg: Decimal
    delta: int
    below_avg: bool
    sla_violations: int


class _Neighbor(TypedDict):
    rank: int
    total_score: Decimal
    gap: Decimal


class _ScorecardStats(TypedDict):
    team_count: int
    category_ranks: dict[str, _CategoryRank]
    service_stats: list[_ServiceStat]
    inject_stats: list[_InjectStat]
    neighbors: list[_Neighbor]
    insights: list[str]
    total_delta: int


def _compute_scorecard_stats(
    score: Standing,
    standings: list[Standing],
    peer_context: ScorecardPeerContext | None = None,
) -> _ScorecardStats:
    """Compute comparative statistics for a team's scorecard against the ranked teams."""
    team = score.team
    ranked = get_leaderboard(standings)
    team_count = len(ranked)

    categories: list[tuple[str, str]] = [
        ("service_points", "services"),
        ("inject_points", "injects"),
        ("orange_points", "orange"),
        ("red_deductions", "red"),
        ("sla_penalties", "sla"),
        ("incident_recovery_points", "recovery"),
        ("point_adjustments", "adjustments"),
    ]

    category_ranks: dict[str, _CategoryRank] = {}
    for field, label in categories:
        value: Decimal = getattr(score, field)
        values: list[Decimal] = [getattr(s, field) for s in ranked]
        mx = max(values, default=Decimal("0"))
        mn = min(values, default=Decimal("0"))
        # Skip categories where nobody has any data
        if mx == 0 and mn == 0:
            continue
        avg = sum(values, Decimal("0")) / len(values)

        # Rank = teams scoring strictly better + 1. Red deductions are negative, so a
        # greater value (closer to 0) is better there too.
        rank = sum(1 for v in values if v > value) + 1
        delta = value - avg

        category_ranks[label] = _CategoryRank(
            rank=rank,
            avg=avg,
            min=mn,
            max=mx,
            value=value,
            delta=int(round(delta)),
            below_avg=delta < 0,
        )

    if peer_context is None:
        peer_context = build_peer_context(standings, extra_team_ids={team.pk})

    inject_stats: list[_InjectStat] = []
    for inj in peer_context.injects_by_team.get(team.pk, ()):
        peers = peer_context.peer_inject_points[inj.inject_id]
        inj_avg = sum(peers, Decimal("0")) / len(peers) if peers else Decimal("0")
        inj_max = max(peers, default=Decimal("0"))
        inj_rank = sum(1 for p in peers if p > inj.points_awarded) + 1
        inj_delta = inj.points_awarded - inj_avg
        inject_stats.append(
            _InjectStat(
                name=inj.inject_name,
                points=inj.points_awarded,
                rank=inj_rank,
                avg=inj_avg,
                max=inj_max,
                delta=int(round(inj_delta)),
                below_avg=inj_delta < 0,
                feedback=inj.feedback if inj.feedback_approved else "",
            )
        )

    service_stats: list[_ServiceStat] = []
    for svc in peer_context.services_by_team.get(team.pk, ()):
        peers = peer_context.peer_service_points[svc.service_name]
        svc_avg = sum(peers, Decimal("0")) / len(peers) if peers else Decimal("0")
        svc_rank = sum(1 for p in peers if p > svc.points) + 1
        svc_delta = svc.points - svc_avg
        service_stats.append(
            _ServiceStat(
                name=svc.service_name,
                points=svc.points,
                rank=svc_rank,
                avg=svc_avg,
                delta=int(round(svc_delta)),
                below_avg=svc_delta < 0,
                sla_violations=svc.sla_violations,
            )
        )

    insights: list[str] = []

    # Best and worst category (by rank, lower is better; tiebreak by distance above avg)
    if category_ranks:
        main_cats = {"services", "injects", "orange"}
        positive_cats = {k: v for k, v in category_ranks.items() if k in main_cats and v["max"] != 0}
        if positive_cats:

            def _cat_sort_key(k: str) -> tuple[int, Decimal]:
                v = positive_cats[k]
                return (v["rank"], -(v["value"] - v["avg"]))

            sorted_cats = sorted(positive_cats, key=_cat_sort_key)
            best_cat = sorted_cats[0]
            best_rank = positive_cats[best_cat]["rank"]
            insights.append(f"Strongest category: {best_cat.title()} (rank #{best_rank} of {team_count})")

    if score.sla_penalties and score.sla_penalties < 0:
        sla_avg = sum((s.sla_penalties for s in ranked), Decimal("0")) / team_count if team_count else Decimal("0")
        if score.sla_penalties < sla_avg:
            insights.append(f"SLA penalties ({score.sla_penalties}) are worse than average ({sla_avg:.0f})")

    if service_stats:
        best_svc = min(service_stats, key=lambda s: (s["rank"], -s["delta"]))
        worst_svc = max(service_stats, key=lambda s: (s["rank"], -s["delta"]))
        if best_svc["name"] != worst_svc["name"]:
            insights.append(f"Best service: {best_svc['name']} (rank #{best_svc['rank']})")
            insights.append(f"Weakest service: {worst_svc['name']} (rank #{worst_svc['rank']})")

    # Nearest competitors (team directly above and below by rank)
    neighbors: list[_Neighbor] = []
    if score.rank:
        neighbors = [
            _Neighbor(rank=ns.rank, total_score=ns.total_score, gap=ns.total_score - score.total_score)
            for ns in ranked
            if ns.rank is not None and ns.team != team and abs(ns.rank - score.rank) <= 1
        ]

    total_avg = sum((s.total_score for s in ranked), Decimal("0")) / team_count if team_count else Decimal("0")
    total_delta = int(round(score.total_score - total_avg))

    return _ScorecardStats(
        team_count=team_count,
        category_ranks=category_ranks,
        service_stats=service_stats,
        inject_stats=inject_stats,
        neighbors=neighbors,
        insights=insights,
        total_delta=total_delta,
    )


def build_scorecard_context(
    score: Standing,
    standings: list[Standing],
    peer_context: ScorecardPeerContext | None = None,
) -> dict[str, object]:
    """Context shared by the scorecard page, its PDF, and the emailed and bulk-exported PDFs."""
    team = score.team

    red_scores = (
        RedTeamScore.objects.filter(affected_teams=team, is_approved=True)
        .select_related("attack_type")
        .order_by("attack_type__name", "pk")
    )

    stats = _compute_scorecard_stats(score, standings, peer_context=peer_context)
    if score.scaling:
        scaling = dict(score.scaling)
    else:
        detailed = calculate_team_score_detailed(team)
        scaling = {key: detailed[key] for key in SCALING_FIELDS}  # type: ignore[literal-required]

    orange_scores = OrangeTeamScore.objects.filter(team=team, is_approved=True).order_by("description", "pk")

    return {
        "team": team,
        "score": score,
        "red_scores": red_scores,
        "stats": stats,
        "red_total": sum(r.points_per_team for r in red_scores),
        "inject_total": sum(i["points"] for i in stats["inject_stats"]),
        "orange_scores": orange_scores,
        "orange_total": sum(o.points_awarded for o in orange_scores),
        "service_total": sum(s["points"] for s in stats["service_stats"]),
        "service_sla_total": sum(s["sla_violations"] for s in stats["service_stats"]),
        "scaling": scaling,
    }
