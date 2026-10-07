from decimal import Decimal
from typing import TypedDict

import weasyprint
from django.db.models import Avg, Max
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string

from core.auth_utils import has_permission, require_permission

from ..calculator import Standing, calculate_team_score_detailed, compute_standings, get_leaderboard, get_standing
from ..models import InjectScore, OrangeTeamScore, RedTeamScore, ServiceDetail


class _CategoryRank(TypedDict):
    rank: int
    avg: Decimal
    min: Decimal
    max: Decimal
    value: Decimal


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
        },
    )


def _compute_scorecard_stats(score: Standing, standings: list[Standing]) -> _ScorecardStats:
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

        if label == "red":
            # Store as absolute values; swap min/max so max = most deductions
            category_ranks[label] = _CategoryRank(
                rank=rank,
                avg=abs(avg),
                min=abs(mx),
                max=abs(mn),
                value=abs(value),
            )
        else:
            category_ranks[label] = _CategoryRank(rank=rank, avg=avg, min=mn, max=mx, value=value)

    # Use the same population as category ranking: only ranked, non-excluded teams
    ranked_team_ids = {s.team.pk for s in ranked}

    inject_stats: list[_InjectStat] = []
    team_injects = (
        InjectScore.objects.filter(team=team, is_approved=True)
        .exclude(inject_id="qualifier-total")
        .order_by("inject_name")
    )

    for inj in team_injects:
        all_inj = InjectScore.objects.filter(inject_id=inj.inject_id, is_approved=True, team_id__in=ranked_team_ids)
        inj_aggs = all_inj.aggregate(avg=Avg("points_awarded"), mx=Max("points_awarded"))
        inj_rank = all_inj.filter(points_awarded__gt=inj.points_awarded).count() + 1
        inj_avg = inj_aggs["avg"] or Decimal("0")
        inj_delta = inj.points_awarded - inj_avg
        inject_stats.append(
            _InjectStat(
                name=inj.inject_name,
                points=inj.points_awarded,
                rank=inj_rank,
                avg=inj_avg,
                max=inj_aggs["mx"] or Decimal("0"),
                delta=int(round(inj_delta)),
                below_avg=inj_delta < 0,
                feedback=inj.feedback if inj.feedback_approved else "",
            )
        )

    service_stats: list[_ServiceStat] = []
    team_services = ServiceDetail.objects.filter(team=team).order_by("service_name")

    for svc in team_services:
        all_svc = ServiceDetail.objects.filter(service_name=svc.service_name, team_id__in=ranked_team_ids)
        svc_aggs = all_svc.aggregate(avg=Avg("points"))
        svc_rank = all_svc.filter(points__gt=svc.points).count() + 1
        svc_avg = svc_aggs["avg"] or Decimal("0")
        svc_delta = svc.points - svc_avg
        service_stats.append(
            _ServiceStat(
                name=svc.service_name,
                points=svc.points,
                rank=svc_rank,
                avg=svc_avg,
                delta=int(round(svc_delta)),
                below_avg=svc_delta < 0,
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

    return _ScorecardStats(
        team_count=team_count,
        category_ranks=category_ranks,
        service_stats=service_stats,
        inject_stats=inject_stats,
        neighbors=neighbors,
        insights=insights,
    )


def build_scorecard_context(score: Standing, standings: list[Standing]) -> dict[str, object]:
    """Context shared by the scorecard page, its PDF, and the emailed and bulk-exported PDFs."""
    team = score.team

    red_scores = (
        RedTeamScore.objects.filter(affected_teams=team, is_approved=True)
        .select_related("attack_type")
        .order_by("attack_type__name", "pk")
    )

    stats = _compute_scorecard_stats(score, standings)
    detailed = calculate_team_score_detailed(team)

    orange_scores = OrangeTeamScore.objects.filter(team=team, is_approved=True).order_by("description")

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
        "scaling": {
            "service_raw": detailed["service_raw"],
            "inject_raw": detailed["inject_raw"],
            "orange_raw": detailed["orange_raw"],
            "service_modifier": detailed["service_modifier"],
            "inject_modifier": detailed["inject_modifier"],
            "orange_modifier": detailed["orange_modifier"],
            "service_weight": detailed["service_weight"],
            "inject_weight": detailed["inject_weight"],
            "orange_weight": detailed["orange_weight"],
        },
    }


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
    context = _scorecard_context(team_number)
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
    html_string = render_to_string("scoring/scorecard_print.html", context, request=request)
    pdf_bytes = weasyprint.HTML(string=html_string).write_pdf()
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="team-{team_number:02d}-scorecard.pdf"'
    return response
