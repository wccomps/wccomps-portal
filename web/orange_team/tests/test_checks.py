import pytest
from django.contrib.auth.models import User
from django.test import Client

from core.models import UserGroups
from orange_team.models import OrangeAssignment, OrangeCheck, OrangeCheckCriterion, OrangeCheckIn
from team.models import Team

pytestmark = pytest.mark.django_db


class TestCheckList:
    def test_lead_can_access(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get("/orange-team/checks/")
        assert response.status_code == 200

    def test_non_lead_denied(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.login(username="orange1", password="test")
        response = client.get("/orange-team/checks/")
        assert response.status_code == 302

    def test_shows_checks_in_list(self) -> None:
        user = User.objects.create_user(username="gold2", password="test")
        UserGroups.objects.create(user=user, authentik_id="g2", groups=["WCComps_GoldTeam"])
        OrangeCheck.objects.create(title="Test Check", description="desc", created_by=user)
        client = Client()
        client.login(username="gold2", password="test")
        response = client.get("/orange-team/checks/")
        assert response.status_code == 200
        assert b"Test Check" in response.content

    def test_empty_state_when_no_checks(self) -> None:
        user = User.objects.create_user(username="gold3", password="test")
        UserGroups.objects.create(user=user, authentik_id="g3", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold3", password="test")
        response = client.get("/orange-team/checks/")
        assert response.status_code == 200
        assert b"No checks" in response.content


class TestCheckCreate:
    def test_create_check_with_criteria(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold1", password="test")
        response = client.post(
            "/orange-team/checks/create/",
            {
                "title": "Password Reset",
                "description": "Ask team to reset admin password",
                "criterion_label_0": "Fast response",
                "criterion_points_0": "3",
                "criterion_label_1": "Professional",
                "criterion_points_1": "3",
                "criterion_label_2": "Resolved",
                "criterion_points_2": "4",
            },
        )
        assert response.status_code == 302
        check = OrangeCheck.objects.get(title="Password Reset")
        assert check.criteria.count() == 3
        assert check.max_score == 10

    def test_create_requires_title(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold1", password="test")
        response = client.post(
            "/orange-team/checks/create/",
            {
                "title": "",
                "description": "test",
                "criterion_label_0": "Fast",
                "criterion_points_0": "3",
            },
        )
        assert response.status_code == 200  # re-renders form with error

    def test_create_without_criteria_uses_max_points(self) -> None:
        # Criteria are optional; a check can define its max directly instead.
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold1", password="test")
        response = client.post(
            "/orange-team/checks/create/",
            {
                "title": "Test Check",
                "description": "test",
                "max_points": "20",
            },
        )
        assert response.status_code == 302
        check = OrangeCheck.objects.get(title="Test Check")
        assert check.criteria.count() == 0
        assert check.max_score == 20

    def test_get_shows_form(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get("/orange-team/checks/create/")
        assert response.status_code == 200
        assert b"Create Check" in response.content

    def test_non_lead_denied(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.login(username="orange1", password="test")
        response = client.post(
            "/orange-team/checks/create/",
            {
                "title": "Test",
                "criterion_label_0": "Fast",
                "criterion_points_0": "3",
            },
        )
        assert response.status_code == 302


class TestCheckDetail:
    def test_view_detail(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Test", description="Desc", created_by=user)
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get(f"/orange-team/checks/{check.pk}/")
        assert response.status_code == 200
        assert b"Test" in response.content

    def test_non_lead_denied(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        client = Client()
        client.login(username="orange1", password="test")
        response = client.get(f"/orange-team/checks/{check.pk}/")
        assert response.status_code == 302

    def test_shows_criteria(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Test", description="Desc", created_by=user)
        OrangeCheckCriterion.objects.create(orange_check=check, label="Fast response", points=5)
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get(f"/orange-team/checks/{check.pk}/")
        assert response.status_code == 200
        assert b"Fast response" in response.content


class TestCheckDuplicate:
    def test_duplicate_creates_copy(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Original", description="Desc", created_by=user)
        OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        client = Client()
        client.login(username="gold1", password="test")
        response = client.post(f"/orange-team/checks/{check.pk}/duplicate/")
        assert response.status_code == 302
        copy = OrangeCheck.objects.get(title="Original (copy)")
        assert copy.criteria.count() == 1

    def test_get_redirects(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Original", description="Desc", created_by=user)
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get(f"/orange-team/checks/{check.pk}/duplicate/")
        assert response.status_code == 302


class TestCheckAssign:
    def test_auto_assign_distributes_teams(self) -> None:
        lead = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="g1", groups=["WCComps_GoldTeam"])
        o1 = User.objects.create_user(username="orange1")
        o2 = User.objects.create_user(username="orange2")
        OrangeCheckIn.objects.create(user=o1)
        OrangeCheckIn.objects.create(user=o2)
        check = OrangeCheck.objects.create(title="Test", description="Desc", created_by=lead)
        OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        Team.objects.create(team_number=1, team_name="T1")
        Team.objects.create(team_number=2, team_name="T2")
        Team.objects.create(team_number=3, team_name="T3")
        Team.objects.create(team_number=4, team_name="T4")
        client = Client()
        client.login(username="gold1", password="test")
        response = client.post(f"/orange-team/checks/{check.pk}/auto-assign/")
        assert response.status_code == 302
        assert OrangeAssignment.objects.filter(orange_check=check).count() == 4
        # Each assignment should have result rows for the criteria check
        for assignment in OrangeAssignment.objects.filter(orange_check=check):
            assert assignment.results.count() == 1

    def test_auto_assign_no_checked_in_users_is_noop(self) -> None:
        lead = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Test", description="Desc", created_by=lead)
        Team.objects.create(team_number=1, team_name="T1")
        client = Client()
        client.login(username="gold1", password="test")
        response = client.post(f"/orange-team/checks/{check.pk}/auto-assign/")
        assert response.status_code == 302
        assert OrangeAssignment.objects.filter(orange_check=check).count() == 0

    def test_assign_get_renders_grid(self) -> None:
        lead = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Test", description="Desc", created_by=lead)
        Team.objects.create(team_number=1, team_name="T1")
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get(f"/orange-team/checks/{check.pk}/assign/")
        assert response.status_code == 200
        assert b"Team 1" in response.content


class TestCheckEdit:
    def test_edit_get_shows_form(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Original", description="Desc", created_by=user)
        OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        client = Client()
        client.login(username="gold1", password="test")
        response = client.get(f"/orange-team/checks/{check.pk}/edit/")
        assert response.status_code == 200
        assert b"Edit Check" in response.content

    def test_edit_updates_check(self) -> None:
        user = User.objects.create_user(username="gold1", password="test")
        UserGroups.objects.create(user=user, authentik_id="g1", groups=["WCComps_GoldTeam"])
        check = OrangeCheck.objects.create(title="Original", description="Desc", created_by=user)
        OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        client = Client()
        client.login(username="gold1", password="test")
        response = client.post(
            f"/orange-team/checks/{check.pk}/edit/",
            {
                "title": "Updated Title",
                "description": "Updated Desc",
                "criterion_label_0": "New C1",
                "criterion_points_0": "10",
            },
        )
        assert response.status_code == 302
        check.refresh_from_db()
        assert check.title == "Updated Title"
        assert check.description == "Updated Desc"
        assert check.criteria.count() == 1
        criterion = check.criteria.first()
        assert criterion is not None
        assert criterion.label == "New C1"
        assert criterion.points == 10
