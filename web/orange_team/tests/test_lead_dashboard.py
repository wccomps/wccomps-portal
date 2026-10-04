from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.test import Client

from orange_team.models import OrangeAssignment, OrangeCheck, OrangeCheckIn
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead_client(create_user_with_groups: Callable[..., User]) -> Client:
    c = Client()
    c.force_login(create_user_with_groups("orangelead", ["WCComps_OrangeTeam_Lead"]))
    return c


def test_dashboard_shows_coverage_and_volunteers(
    lead_client: Client, create_user_with_groups: Callable[..., User]
) -> None:
    vol = create_user_with_groups("v1", ["WCComps_OrangeTeam"])
    OrangeCheckIn.objects.create(user=vol)
    check = OrangeCheck.objects.create(title="Firewall", description="d", max_points=10)
    team = Team.objects.create(team_number=1, team_name="T1")
    OrangeAssignment.objects.create(orange_check=check, user=vol, team=team, status="approved", score=8)
    resp = lead_client.get("/orange-team/checks/")
    assert resp.status_code == 200
    assert b"Firewall" in resp.content
    assert b"v1" in resp.content


def test_auto_assign_all_creates_assignments(lead_client: Client, create_user_with_groups: Callable[..., User]) -> None:
    vol = create_user_with_groups("v1", ["WCComps_OrangeTeam"])
    OrangeCheckIn.objects.create(user=vol)
    OrangeCheck.objects.create(title="C1", description="d")
    OrangeCheck.objects.create(title="C2", description="d")
    Team.objects.create(team_number=1, team_name="T1")
    Team.objects.create(team_number=2, team_name="T2")
    resp = lead_client.post("/orange-team/checks/auto-assign/")
    assert resp.status_code == 302
    assert OrangeAssignment.objects.count() == 4
