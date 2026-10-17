import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from core.models import UserGroups
from orange_team.models import (
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
)
from team.models import Team

pytestmark = pytest.mark.django_db


class TestAssignmentSave:
    def _setup(
        self,
    ) -> tuple[
        User,
        OrangeAssignment,
        OrangeCheckCriterion,
        OrangeCheckCriterion,
        OrangeAssignmentResult,
        OrangeAssignmentResult,
    ]:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        c1 = OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        c2 = OrangeCheckCriterion.objects.create(orange_check=check, label="C2", points=3)
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        r1 = OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c1)
        r2 = OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c2)
        return user, assignment, c1, c2, r1, r2

    def test_save_criterion(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert response.status_code == 200
        data = response.json()
        assert data["score"] == 5
        assert data["max_score"] == 8
        r1.refresh_from_db()
        assert r1.met is True

    def test_save_updates_status_to_in_progress(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        assert assignment.status == "pending"
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assignment.refresh_from_db()
        assert assignment.status == "in_progress"

    def test_cannot_save_submitted_assignment(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        assignment.status = "submitted"
        assignment.save()
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_other_user_cannot_save(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        other = User.objects.create_user(username="orange2", password="test")
        UserGroups.objects.create(user=other, authentik_id="o2", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange2"))
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert response.status_code == 404


class TestAssignmentSubmit:
    def test_submit_assignment(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        c1 = OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c1, met=True)
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(f"/orange-team/assignments/{assignment.pk}/submit/")
        assert response.status_code == 302
        assignment.refresh_from_db()
        assert assignment.status == "submitted"
        assert assignment.score == 5
        assert assignment.submitted_at is not None

    def test_cannot_submit_twice(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team, status="submitted")
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(f"/orange-team/assignments/{assignment.pk}/submit/")
        assert response.status_code == 302  # redirects with error message
