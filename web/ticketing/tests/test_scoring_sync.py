from decimal import Decimal

import pytest
from scoring.calculator import calculate_team_score_detailed, compute_standings
from scoring.models import ServiceScore

from team.models import Team
from ticketing.models import Ticket

pytestmark = pytest.mark.django_db

_counter = [0]


def _ticket(team: Team, points: int, approved: bool) -> Ticket:
    _counter[0] += 1
    return Ticket.objects.create(
        ticket_number=f"TS-{_counter[0]:04d}",
        team=team,
        title="t",
        status="resolved",
        points_charged=points,
        is_approved=approved,
    )


def test_calculator_sums_approved_ticket_charges_negative() -> None:
    team = Team.objects.create(team_number=1, team_name="T1")
    _ticket(team, 30, True)
    _ticket(team, 20, True)
    _ticket(team, 99, False)  # unapproved - ignored
    breakdown = calculate_team_score_detailed(team)
    assert breakdown["point_adjustments"] == Decimal("-50")
    assert breakdown["total_score"] == Decimal("-50.00")


def test_calculator_counts_ticket_adjustments_without_servicescore() -> None:
    team = Team.objects.create(team_number=2, team_name="T2", is_active=True)
    _ticket(team, 10, True)
    assert not ServiceScore.objects.filter(team=team).exists()
    standings = compute_standings()
    assert len(standings) == 1
    assert standings[0].point_adjustments == Decimal("-10")
    assert standings[0].rank == 1


def test_calculator_combines_service_points_and_ticket_adjustments() -> None:
    team = Team.objects.create(team_number=3, team_name="T3")
    ServiceScore.objects.create(team=team, service_points=Decimal("500"))
    _ticket(team, 40, True)
    breakdown = calculate_team_score_detailed(team)
    assert breakdown["service_raw"] == Decimal("500")
    assert breakdown["point_adjustments"] == Decimal("-40")


def test_reopen_leaves_approved_adjustment_intact(admin_user) -> None:
    from django.test import Client
    from django.urls import reverse

    team = Team.objects.create(team_number=11, team_name="T11")
    t = _ticket(team, 25, approved=True)
    assert calculate_team_score_detailed(team)["point_adjustments"] == Decimal("-25")
    client = Client()
    client.force_login(admin_user)
    client.post(reverse("ticket_reopen", args=[t.ticket_number]), {})
    # An approved ticket cannot be reopened, so its charge stays deducted.
    t.refresh_from_db()
    assert t.is_approved
    assert calculate_team_score_detailed(team)["point_adjustments"] == Decimal("-25")

