from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from core.models import UserGroups
from orange_team.models import OrangeAssignment, OrangeCheck, OrangeFollowUp
from team.models import Team

pytestmark = pytest.mark.django_db


class TestFollowUpCreate:
    def test_create_followup(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        client = Client()
        client.login(username="orange1", password="test")
        response = client.post(
            "/orange-team/followups/create/",
            {
                "assignment_id": assignment.pk,
                "minutes": "15",
                "note": "Check back on password fix",
            },
        )
        assert response.status_code == 302
        assert OrangeFollowUp.objects.filter(user=user, assignment=assignment).exists()
        followup = OrangeFollowUp.objects.get(user=user, assignment=assignment)
        assert followup.note == "Check back on password fix"
        assert not followup.dismissed

    def test_other_user_cannot_create(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        other = User.objects.create_user(username="orange2")
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(orange_check=check, user=other, team=team)
        client = Client()
        client.login(username="orange1", password="test")
        response = client.post(
            "/orange-team/followups/create/",
            {
                "assignment_id": assignment.pk,
                "minutes": "15",
            },
        )
        assert response.status_code == 404


class TestFollowUpDismiss:
    def test_dismiss_followup(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        followup = OrangeFollowUp.objects.create(
            user=user, assignment=assignment, remind_at=timezone.now() + timedelta(minutes=15)
        )
        client = Client()
        client.login(username="orange1", password="test")
        response = client.post(f"/orange-team/followups/{followup.pk}/dismiss/")
        assert response.status_code == 302
        followup.refresh_from_db()
        assert followup.dismissed

    def test_other_user_cannot_dismiss(self) -> None:
        user = User.objects.create_user(username="orange1")
        other = User.objects.create_user(username="orange2", password="test")
        UserGroups.objects.create(user=other, authentik_id="o2", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        followup = OrangeFollowUp.objects.create(
            user=user, assignment=assignment, remind_at=timezone.now() + timedelta(minutes=15)
        )
        client = Client()
        client.login(username="orange2", password="test")
        response = client.post(f"/orange-team/followups/{followup.pk}/dismiss/")
        assert response.status_code == 404


def test_the_follow_up_input_allows_what_the_form_allows() -> None:
    from orange_team.forms import FollowUpForm
    from orange_team.models import OrangeCheckIn

    user = User.objects.create_user(username="orange1", password="test")
    UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
    OrangeCheckIn.objects.create(user=user)
    team = Team.objects.create(team_number=1, team_name="T1")
    check = OrangeCheck.objects.create(title="Test", description="Desc", scheduled_at=timezone.now())
    OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
    client = Client()
    client.force_login(user)

    page = client.get("/orange-team/").content.decode()

    limit = FollowUpForm.base_fields["minutes"].max_value
    assert 'name="minutes"' in page
    assert f'max="{limit}"' in page
    assert not FollowUpForm({"assignment_id": "1", "minutes": str(limit + 1)}).is_valid()
