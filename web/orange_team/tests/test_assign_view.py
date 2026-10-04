from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import NoReverseMatch, reverse

from orange_team.models import OrangeAssignment, OrangeCheck, OrangeCheckIn
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def lead_client(create_user_with_groups: Callable[..., User]) -> Client:
    c = Client()
    c.force_login(create_user_with_groups("orangelead", ["WCComps_OrangeTeam_Lead"]))
    return c


def test_assign_page_renders(lead_client: Client) -> None:
    check = OrangeCheck.objects.create(title="Firewall", description="d", max_points=10)
    Team.objects.create(team_number=1, team_name="T1")
    resp = lead_client.get(f"/orange-team/checks/{check.id}/assign/")
    assert resp.status_code == 200
    assert b"Firewall" in resp.content
    assert b"Team 1" in resp.content


def test_auto_assign_from_view(lead_client: Client, create_user_with_groups: Callable[..., User]) -> None:
    vol = create_user_with_groups("v1", ["WCComps_OrangeTeam"])
    OrangeCheckIn.objects.create(user=vol)
    check = OrangeCheck.objects.create(title="C", description="d")
    Team.objects.create(team_number=1, team_name="T1")
    resp = lead_client.post(f"/orange-team/checks/{check.id}/auto-assign/")
    assert resp.status_code == 302
    assert OrangeAssignment.objects.filter(orange_check=check).count() == 1


def test_reassign_one_team(lead_client: Client, create_user_with_groups: Callable[..., User]) -> None:
    v1 = create_user_with_groups("v1", ["WCComps_OrangeTeam"])
    v2 = create_user_with_groups("v2", ["WCComps_OrangeTeam"])
    for v in (v1, v2):
        OrangeCheckIn.objects.create(user=v)
    check = OrangeCheck.objects.create(title="C", description="d")
    team = Team.objects.create(team_number=1, team_name="T1")
    a = OrangeAssignment.objects.create(orange_check=check, user=v1, team=team)
    resp = lead_client.post(f"/orange-team/assignments/{a.id}/reassign/", {"user_id": str(v2.id)})
    assert resp.status_code == 302
    a.refresh_from_db()
    assert a.user_id == v2.id


def test_reassign_blocked_on_scored(lead_client: Client, create_user_with_groups: Callable[..., User]) -> None:
    v1 = create_user_with_groups("v1", ["WCComps_OrangeTeam"])
    v2 = create_user_with_groups("v2", ["WCComps_OrangeTeam"])
    OrangeCheckIn.objects.create(user=v2)
    check = OrangeCheck.objects.create(title="C", description="d")
    team = Team.objects.create(team_number=1, team_name="T1")
    a = OrangeAssignment.objects.create(orange_check=check, user=v1, team=team, status="approved", score=5)
    lead_client.post(f"/orange-team/assignments/{a.id}/reassign/", {"user_id": str(v2.id)})
    a.refresh_from_db()
    assert a.user_id == v1.id


def test_team_checkins_url_removed() -> None:
    with pytest.raises(NoReverseMatch):
        reverse("orange_team:team_checkins")
