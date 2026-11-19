"""A team's scorecard compares it with the ranked teams: category, inject and service ranks, insights, neighbours."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from scoring.calculator import compute_standings, get_standing
from scoring.models import (
    AttackType,
    IncidentReport,
    InjectScore,
    RedTeamScore,
    ScoringExclusion,
    ScoringTemplate,
    ServiceDetail,
    ServiceScore,
)
from scoring.scorecard import build_scorecard_context
from team.models import Team
from ticketing.models import Ticket

pytestmark = pytest.mark.django_db


@pytest.fixture
def three_teams() -> list[Team]:
    # Service and inject maxes proportional to their weights, no orange checks: scaled points equal raw points.
    ScoringTemplate.objects.create(
        service_weight=Decimal("50"),
        inject_weight=Decimal("50"),
        orange_weight=Decimal("0"),
        service_max=Decimal("100"),
        inject_max=Decimal("100"),
    )
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n}", is_active=True) for n in (1, 2, 3)]
    for team, service, inject, feedback_approved in zip(
        teams, ("300", "300", "100"), ("50", "30", "0"), (True, False, False), strict=True
    ):
        ServiceScore.objects.create(team=team, service_points=Decimal(service))
        InjectScore.objects.create(
            team=team,
            inject_id="inj-a",
            inject_name="Inject A",
            points_awarded=Decimal(inject),
            feedback=f"feedback for {team.team_number}",
            feedback_approved=feedback_approved,
            is_approved=True,
        )
    InjectScore.objects.create(
        team=teams[0], inject_id="inj-b", inject_name="Inject B", points_awarded=Decimal("40"), is_approved=False
    )
    for team, web, dns, sla in zip(teams, ("200", "100", "0"), ("100", "200", "100"), (2, 0, 0), strict=True):
        ServiceDetail.objects.create(team=team, service_name="web", points=Decimal(web))
        ServiceDetail.objects.create(team=team, service_name="dns", points=Decimal(dns), sla_violations=sla)
    return teams


def _context(team: Team) -> dict:
    standings = compute_standings()
    return build_scorecard_context(get_standing(team.team_number, standings), standings)


def test_category_ranks_share_a_tie_and_count_a_zero(three_teams) -> None:
    stats = _context(three_teams[0])["stats"]

    assert stats["team_count"] == 3
    assert set(stats["category_ranks"]) == {"services", "injects"}
    services = stats["category_ranks"]["services"]
    assert (services["rank"], services["max"], services["min"], services["delta"], services["below_avg"]) == (
        1,
        Decimal("300"),
        Decimal("100"),
        67,
        False,
    )
    injects = stats["category_ranks"]["injects"]
    assert (injects["rank"], injects["min"], injects["delta"]) == (1, Decimal("0"), 23)
    assert _context(three_teams[1])["stats"]["category_ranks"]["services"]["rank"] == 1
    assert _context(three_teams[2])["stats"]["category_ranks"]["injects"]["below_avg"] is True


def test_inject_stats_list_approved_grades_with_approved_feedback(three_teams) -> None:
    first = _context(three_teams[0])
    third = _context(three_teams[2])

    [inject] = first["stats"]["inject_stats"]
    assert (inject["name"], inject["points"], inject["rank"], inject["max"], inject["delta"]) == (
        "Inject A",
        Decimal("50"),
        1,
        Decimal("50"),
        23,
    )
    assert inject["feedback"] == "feedback for 1"
    assert first["inject_total"] == Decimal("50")
    [third_inject] = third["stats"]["inject_stats"]
    assert (third_inject["rank"], third_inject["feedback"], third_inject["below_avg"]) == (3, "", True)


def test_service_stats_and_insights_name_the_best_and_weakest_service(three_teams) -> None:
    context = _context(three_teams[0])
    stats = context["stats"]

    by_name = {service["name"]: service for service in stats["service_stats"]}
    assert (by_name["web"]["rank"], by_name["web"]["delta"], by_name["web"]["below_avg"]) == (1, 100, False)
    assert (by_name["dns"]["rank"], by_name["dns"]["delta"], by_name["dns"]["below_avg"]) == (2, -33, True)
    assert (context["service_total"], context["service_sla_total"]) == (Decimal("300"), 2)
    assert stats["insights"] == [
        "Strongest category: Services (rank #1 of 3)",
        "Best service: web (rank #1)",
        "Weakest service: dns (rank #2)",
    ]


def test_neighbours_and_total_delta(three_teams) -> None:
    first = _context(three_teams[0])["stats"]
    second = _context(three_teams[1])["stats"]

    assert first["neighbors"] == [{"rank": 2, "total_score": Decimal("330.00"), "gap": Decimal("-20.00")}]
    assert [n["rank"] for n in second["neighbors"]] == [1, 3]
    # Totals 350, 330 and 100 average 260.
    assert (first["total_delta"], second["total_delta"]) == (90, 70)


def test_sla_penalties_worse_than_average_are_an_insight(three_teams) -> None:
    ServiceScore.objects.filter(team=three_teams[0]).update(sla_violations=Decimal("-10"))
    ServiceScore.objects.filter(team=three_teams[1]).update(sla_violations=Decimal("-2"))

    first = _context(three_teams[0])["stats"]["insights"]
    second = _context(three_teams[1])["stats"]["insights"]

    assert "SLA penalties (-10.00) are worse than average (-4)" in first
    assert not any(insight.startswith("SLA") for insight in second)


def test_red_findings_are_the_teams_approved_ones_by_attack_type(three_teams) -> None:
    red = User.objects.create_user("red")
    sqli, xss = AttackType.objects.create(name="SQL injection"), AttackType.objects.create(name="XSS")

    def finding(attack_type: AttackType, points: str, teams: list[Team], approved: bool = True) -> RedTeamScore:
        row = RedTeamScore.objects.create(
            attack_type=attack_type, points_per_team=Decimal(points), submitted_by=red, is_approved=approved
        )
        row.affected_teams.set(teams)
        return row

    by_xss = finding(xss, "25", [three_teams[0]])
    by_sqli = finding(sqli, "50", three_teams[:2])
    finding(sqli, "100", [three_teams[0]], approved=False)
    finding(xss, "200", [three_teams[1]])

    context = _context(three_teams[0])

    assert list(context["red_scores"]) == [by_sqli, by_xss]
    assert context["red_total"] == Decimal("75")


def test_every_scored_category_is_ranked_even_after_an_empty_one(three_teams) -> None:
    # Orange (nobody scored) comes before red, SLA, recovery and adjustments in the category list.
    red = User.objects.create_user("red")
    ServiceScore.objects.filter(team=three_teams[0]).update(sla_violations=Decimal("-10"))
    finding = RedTeamScore.objects.create(points_per_team=Decimal("25"), submitted_by=red, is_approved=True)
    finding.affected_teams.add(three_teams[2])
    IncidentReport.objects.create(
        team=three_teams[1],
        submitted_by=red,
        attack_description="x",
        source_ip="10.0.0.1",
        attack_detected_at=timezone.now(),
        points_returned=Decimal("30"),
        is_approved=True,
    )
    Ticket.objects.create(
        ticket_number="T-0001", team=three_teams[0], title="t", status="resolved", points_charged=20, is_approved=True
    )

    ranks = _context(three_teams[0])["stats"]["category_ranks"]

    assert set(ranks) == {"services", "injects", "red", "sla", "recovery", "adjustments"}
    assert (ranks["sla"]["value"], ranks["recovery"]["value"], ranks["adjustments"]["value"]) == (
        Decimal("-10"),
        Decimal("0"),
        Decimal("-20"),
    )


def test_a_team_exactly_at_the_average_is_not_below_it(three_teams) -> None:
    ServiceScore.objects.filter(team=three_teams[1]).update(service_points=Decimal("200"))
    for team, points in zip(three_teams, ("40", "20", "0"), strict=True):
        InjectScore.objects.create(
            team=team, inject_id="inj-c", inject_name="Inject C", points_awarded=Decimal(points), is_approved=True
        )

    stats = _context(three_teams[1])["stats"]

    services = stats["category_ranks"]["services"]
    assert (services["delta"], services["below_avg"]) == (0, False)
    inject_c = next(inject for inject in stats["inject_stats"] if inject["name"] == "Inject C")
    assert (inject_c["delta"], inject_c["below_avg"]) == (0, False)


def test_an_excluded_teams_scorecard_rates_its_injects_against_the_ranked_teams(three_teams) -> None:
    ScoringExclusion.objects.create(team=three_teams[2])
    InjectScore.objects.create(
        team=three_teams[2], inject_id="solo", inject_name="Solo", points_awarded=Decimal("5"), is_approved=True
    )

    by_name = {inject["name"]: inject for inject in _context(three_teams[2])["stats"]["inject_stats"]}

    solo = by_name["Solo"]
    assert (solo["rank"], solo["avg"], solo["max"], solo["delta"]) == (1, Decimal("0"), Decimal("0"), 5)
    shared = by_name["Inject A"]
    assert (shared["rank"], shared["avg"], shared["max"]) == (3, Decimal("40"), Decimal("50"))
