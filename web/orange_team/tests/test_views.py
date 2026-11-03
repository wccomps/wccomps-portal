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
        client.force_login(User.objects.get(username="blue1"))
        response = client.get("/orange-team/")
        assert response.status_code == 403

    def test_orange_team_can_access(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange1-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"My Checks" in response.content

    def test_gold_team_can_access(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="gold1-uid", groups=["WCComps_GoldTeam"])
        client = Client()
        client.force_login(User.objects.get(username="gold1"))
        response = client.get("/orange-team/")
        assert response.status_code == 200

    def test_gold_team_sees_lead_link(self) -> None:
        user = User.objects.create_user(username="gold2", password="test")
        UserGroups.objects.create(user=user, authentik_id="gold2-uid", groups=["WCComps_GoldTeam"])
        client = Client()
        client.force_login(User.objects.get(username="gold2"))
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"Manage Checks" in response.content

    def test_orange_team_no_lead_link(self) -> None:
        user = User.objects.create_user(username="orange2", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange2-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange2"))
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"Manage Checks" not in response.content

    def test_empty_assignments_shows_empty_state(self) -> None:
        user = User.objects.create_user(username="orange3", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange3-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange3"))
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"No checks" in response.content

    def test_checked_out_by_default(self) -> None:
        user = User.objects.create_user(username="orange4", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange4-uid", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange4"))
        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b"Checked Out" in response.content
        assert b"Check In" in response.content

    def test_assigned_check_shows_multiline_description(self) -> None:
        from orange_team.models import OrangeAssignment, OrangeCheck
        from team.models import Team

        user = User.objects.create_user(username="orange5", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange5-uid", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="Team 01")
        check = OrangeCheck.objects.create(title="Phone Check", description="1. Dial extension\n2. Verify greeting")
        OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        client = Client()
        client.force_login(user)

        response = client.get("/orange-team/")
        assert response.status_code == 200
        assert b'<div class="text-sm text-muted whitespace-pre-wrap">1. Dial extension\n2. Verify greeting</div>' in (
            response.content
        )

    def test_dashboard_shows_assignment_status_and_window_badges(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from orange_team.models import OrangeAssignment, OrangeCheck
        from team.models import Team

        user = User.objects.create_user(username="orange6", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange6-uid", groups=["WCComps_OrangeTeam"])
        now = timezone.now()
        t1 = Team.objects.create(team_number=1, team_name="Team 01")
        t2 = Team.objects.create(team_number=2, team_name="Team 02")
        live_check = OrangeCheck.objects.create(
            title="Live Check",
            description="desc",
            scheduled_at=now - timedelta(minutes=5),
            time_limit=timedelta(hours=1),
        )
        upcoming_check = OrangeCheck.objects.create(
            title="Upcoming Check",
            description="desc",
            scheduled_at=now + timedelta(hours=1),
            time_limit=timedelta(hours=1),
        )
        OrangeAssignment.objects.create(orange_check=live_check, user=user, team=t1, status="pending")
        OrangeAssignment.objects.create(orange_check=upcoming_check, user=user, team=t2, status="in_progress")

        client = Client()
        client.force_login(user)
        html = client.get("/orange-team/").content.decode()
        assert "Pending" in html
        assert "In Progress" in html
        assert "badge--success" in html
        assert "badge--info" in html
