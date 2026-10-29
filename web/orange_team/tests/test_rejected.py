"""A rejected assignment goes back to its volunteer: on the dashboard with the lead's reason, to edit and resubmit."""

import json
from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from orange_team.models import OrangeAssignment, OrangeAssignmentResult, OrangeCheck, OrangeCheckCriterion
from team.models import Team

pytestmark = pytest.mark.django_db

REASON = "Missing the firewall screenshot"


@pytest.fixture
def volunteer(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("volunteer", ["WCComps_OrangeTeam"])


def _rejected(volunteer: User, *, max_points: int = 10, criteria: bool = True, score: int = 0) -> OrangeAssignment:
    check = OrangeCheck.objects.create(title="Firewall", description="d", max_points=max_points)
    assignment = OrangeAssignment.objects.create(
        orange_check=check,
        user=volunteer,
        team=Team.objects.create(team_number=1, team_name="T1"),
        status="rejected",
        score=score,
        notes=REASON,
        reviewed_by=volunteer,
        reviewed_at=timezone.now(),
    )
    if criteria:
        criterion = OrangeCheckCriterion.objects.create(orange_check=check, label="Rule", points=4)
        OrangeAssignmentResult.objects.create(assignment=assignment, criterion=criterion)
    return assignment


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_a_rejected_assignment_is_on_the_dashboard_with_the_leads_reason(volunteer: User) -> None:
    assignment = _rejected(volunteer)
    response = _client(volunteer).get(reverse("orange_team:dashboard"))
    assert [a.pk for a in response.context["assignments"]] == [assignment.pk]
    assert REASON in response.content.decode()


def test_a_rejected_assignment_can_be_edited_and_resubmitted(volunteer: User) -> None:
    assignment = _rejected(volunteer)
    criterion = assignment.orange_check.criteria.get()
    client = _client(volunteer)
    saved = client.post(
        reverse("orange_team:assignment_save", args=[assignment.pk]),
        json.dumps({"criterion_id": criterion.pk, "met": True}),
        content_type="application/json",
    )
    assert saved.status_code == 200
    client.post(reverse("orange_team:assignment_submit", args=[assignment.pk]))
    assignment.refresh_from_db()
    assert (assignment.status, assignment.score) == ("submitted", 4)
    assert (assignment.reviewed_by, assignment.reviewed_at, assignment.notes) == (None, None, "")


def test_a_resubmit_after_the_maximum_was_lowered_cannot_exceed_it(volunteer: User) -> None:
    assignment = _rejected(volunteer, criteria=False, score=9)
    OrangeCheck.objects.filter(pk=assignment.orange_check_id).update(max_points=5)
    _client(volunteer).post(reverse("orange_team:assignment_submit", args=[assignment.pk]))
    assignment.refresh_from_db()
    assert (assignment.status, assignment.score) == ("submitted", 5)
