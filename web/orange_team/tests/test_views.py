import pytest
from django.contrib.auth.models import User
from django.test import Client

from core.models import UserGroups

pytestmark = pytest.mark.django_db


class TestDashboard:
    def test_anonymous_redirected(self) -> None:
        client = Client()
        response = client.get("/orange-team/")
        assert response.status_code == 302

    def test_non_orange_user_denied(self) -> None:
        user = User.objects.create_user(username="blue1", password="test")
        UserGroups.objects.create(user=user, authentik_id="blue1-uid", groups=["WCComps_BlueTeam01"])
        client = Client()
        client.login(username="blue1", password="test")
        response = client.get("/orange-team/")
        assert response.status_code == 302
        assert response.url == "/"

    def test_orange_team_can_access(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange1-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.login(username="orange1", password="test")
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"My Checks" in response.content

    def test_gold_team_can_access(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="gold1-uid", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get("/orange-team/")
        assert response.status_code == 200

    def test_gold_team_sees_lead_link(self) -> None:
        user = User.objects.create_user(username="gold2", password="test")
        UserGroups.objects.create(user=user, authentik_id="gold2-uid", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold2", password="test")
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"Manage Checks" in response.content

    def test_orange_team_no_lead_link(self) -> None:
        user = User.objects.create_user(username="orange2", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange2-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.login(username="orange2", password="test")
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"Manage Checks" not in response.content

    def test_empty_assignments_shows_empty_state(self) -> None:
        user = User.objects.create_user(username="orange3", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange3-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.login(username="orange3", password="test")
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"No checks" in response.content

    def test_checked_out_by_default(self) -> None:
        user = User.objects.create_user(username="orange4", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange4-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.login(username="orange4", password="test")
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"Checked Out" in response.content
        assert b"Check In" in response.content
