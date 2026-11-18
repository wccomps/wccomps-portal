"""A team's scorecard compares it with the ranked teams: category, inject and service ranks, insights, neighbours."""

from decimal import Decimal

import pytest

from scoring.calculator import compute_standings, get_standing
from scoring.models import InjectScore, ScoringTemplate, ServiceDetail, ServiceScore
from scoring.scorecard import build_scorecard_context
from team.models import Team

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
