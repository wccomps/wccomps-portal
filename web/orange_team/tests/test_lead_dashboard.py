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
    from datetime import timedelta

    from django.utils import timezone

    vol = create_user_with_groups("v1", ["WCComps_OrangeTeam"])
    OrangeCheckIn.objects.create(user=vol)
    now = timezone.now()
    OrangeCheck.objects.create(
        title="C1", description="d", scheduled_at=now - timedelta(minutes=5), time_limit=timedelta(hours=1)
    )
    OrangeCheck.objects.create(
        title="C2", description="d", scheduled_at=now - timedelta(minutes=5), time_limit=timedelta(hours=1)
    )
    Team.objects.create(team_number=1, team_name="T1")
    Team.objects.create(team_number=2, team_name="T2")
    resp = lead_client.post("/orange-team/checks/auto-assign/")
    assert resp.status_code == 302
    assert OrangeAssignment.objects.count() == 4


def test_auto_assign_all_skips_unscheduled_and_closed(
    lead_client: Client, create_user_with_groups: Callable[..., User]
) -> None:
    from datetime import timedelta

    from django.utils import timezone

    vol = create_user_with_groups("v1", ["WCComps_OrangeTeam"])
    OrangeCheckIn.objects.create(user=vol)
    Team.objects.create(team_number=1, team_name="T1")
    now = timezone.now()
    draft = OrangeCheck.objects.create(title="Draft", description="d")  # no schedule
    closed = OrangeCheck.objects.create(
        title="Closed", description="d", scheduled_at=now - timedelta(hours=2), time_limit=timedelta(minutes=30)
    )
    live = OrangeCheck.objects.create(
        title="Live", description="d", scheduled_at=now - timedelta(minutes=5), time_limit=timedelta(hours=1)
    )
    resp = lead_client.post("/orange-team/checks/auto-assign/")
    assert resp.status_code == 302
    assert OrangeAssignment.objects.filter(orange_check=draft).count() == 0
    assert OrangeAssignment.objects.filter(orange_check=closed).count() == 0
    assert OrangeAssignment.objects.filter(orange_check=live).count() == 1
