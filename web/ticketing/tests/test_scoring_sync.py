from decimal import Decimal

import pytest
from scoring.models import ServiceScore

from team.models import Team
from ticketing.models import Ticket
from ticketing.scoring_sync import recompute_all_ticket_adjustments, recompute_team_ticket_adjustment

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


def test_recompute_sums_approved_charges_negative() -> None:
    team = Team.objects.create(team_number=1, team_name="T1")
    _ticket(team, 30, True)
    _ticket(team, 20, True)
    _ticket(team, 99, False)  # unapproved — ignored
    recompute_team_ticket_adjustment(team)
    assert ServiceScore.objects.get(team=team).point_adjustments == Decimal("-50")


def test_recompute_creates_servicescore_if_absent() -> None:
    team = Team.objects.create(team_number=2, team_name="T2")
    _ticket(team, 10, True)
    recompute_team_ticket_adjustment(team)
    assert ServiceScore.objects.filter(team=team).exists()


def test_recompute_preserves_service_points() -> None:
    team = Team.objects.create(team_number=3, team_name="T3")
    ServiceScore.objects.create(team=team, service_points=Decimal("500"))
    _ticket(team, 40, True)
    recompute_team_ticket_adjustment(team)
    ss = ServiceScore.objects.get(team=team)
    assert ss.service_points == Decimal("500")
    assert ss.point_adjustments == Decimal("-40")


def test_verify_view_sets_adjustment(admin_user) -> None:
    from django.test import Client
    from django.urls import reverse

    team = Team.objects.create(team_number=10, team_name="T10")
    t = _ticket(team, 25, approved=False)
    client = Client()
    client.force_login(admin_user)
    resp = client.post(reverse("ops_verify_ticket", args=[t.ticket_number]), {"points_adjustment": "25"})
    assert resp.status_code in (302, 200)
    t.refresh_from_db()
    assert t.is_approved
    assert ServiceScore.objects.get(team=team).point_adjustments == Decimal("-25")


def test_reopen_leaves_approved_adjustment_intact(admin_user) -> None:
    from django.test import Client
    from django.urls import reverse

    team = Team.objects.create(team_number=11, team_name="T11")
    t = _ticket(team, 25, approved=True)
    recompute_team_ticket_adjustment(team)
    assert ServiceScore.objects.get(team=team).point_adjustments == Decimal("-25")
    client = Client()
    client.force_login(admin_user)
    client.post(reverse("ticket_reopen", args=[t.ticket_number]), {})
    # An approved ticket cannot be reopened, so its charge stays deducted.
    t.refresh_from_db()
    assert t.is_approved
    assert ServiceScore.objects.get(team=team).point_adjustments == Decimal("-25")


def test_recompute_all_covers_every_team_with_tickets() -> None:
    t1 = Team.objects.create(team_number=4, team_name="T4")
    t2 = Team.objects.create(team_number=5, team_name="T5")
    Team.objects.create(team_number=6, team_name="T6")  # no tickets
    _ticket(t1, 15, True)
    _ticket(t2, 25, True)
    count = recompute_all_ticket_adjustments()
    assert count == 2
    assert ServiceScore.objects.get(team=t1).point_adjustments == Decimal("-15")
    assert ServiceScore.objects.get(team=t2).point_adjustments == Decimal("-25")
