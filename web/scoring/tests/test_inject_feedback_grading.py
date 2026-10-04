"""Feedback entered on the grading page saves to the grade; only the white team lead's is auto-approved."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from scoring.models import InjectScore
from team.models import Team

pytestmark = pytest.mark.django_db

INJECT = MagicMock(inject_id=1, title="Firewall policy", description="")


@pytest.fixture
def teams():
    ts = [Team.objects.create(team_number=n, team_name=f"Team {n}", is_active=True) for n in (1, 2)]
    reviewer = User.objects.create(username="reviewer")
    first = User.objects.create(username="first-grader")
    g2 = InjectScore.objects.create(
        team=ts[1], inject_id="1", inject_name="Firewall policy", points_awarded=Decimal("7"), graded_by=first
    )
    g2.approve(reviewer)  # team 2 already approved, to prove feedback-only edits don't un-approve
    return ts


def _post(user, data):
    client = Client()
    client.force_login(user)
    with patch("quotient.client.QuotientClient") as q:
        q.return_value.get_injects.return_value = [INJECT]
        return client.post(reverse("scoring:inject_grading") + "?inject=1", {"inject_id": "1", **data}, follow=True)


def test_lead_feedback_is_saved_and_auto_approved(teams, gold_team_user):
    _post(gold_team_user, {"points_team_1": "10", "feedback_team_1": "Great firewall rules"})

    g = InjectScore.objects.get(team__team_number=1)
    assert g.feedback == "Great firewall rules"
    assert g.feedback_approved is True
    assert g.feedback_approved_by == gold_team_user


def test_non_lead_feedback_is_saved_unapproved(teams, white_team_user):
    _post(white_team_user, {"points_team_1": "10", "feedback_team_1": "Needs work"})

    g = InjectScore.objects.get(team__team_number=1)
    assert g.feedback == "Needs work"
    assert g.feedback_approved is False
    assert g.feedback_approved_by is None


def test_feedback_only_edit_keeps_the_score_approval(teams, gold_team_user):
    _post(gold_team_user, {"points_team_2": "7", "feedback_team_2": "Solid report"})

    g = InjectScore.objects.get(team__team_number=2)
    assert g.feedback == "Solid report" and g.feedback_approved is True
    assert g.is_approved is True and g.approved_by.username == "reviewer"
    assert g.graded_by.username == "first-grader"


def test_grading_page_shows_existing_feedback(teams, gold_team_user):
    g = InjectScore.objects.get(team__team_number=2)
    g.feedback = "Prior feedback here"
    g.save(update_fields=["feedback"])
    client = Client()
    client.force_login(gold_team_user)
    with patch("quotient.client.QuotientClient") as q:
        q.return_value.get_injects.return_value = [INJECT]
        response = client.get(reverse("scoring:inject_grading") + "?inject=1")

    assert b"feedback_team_2" in response.content
    assert b"Prior feedback here" in response.content
