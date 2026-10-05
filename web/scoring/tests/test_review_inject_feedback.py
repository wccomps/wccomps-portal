"""The feedback-review page lists graded injects that have feedback (not the unused notes field)."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from scoring.models import InjectScore
from team.models import Team

pytestmark = pytest.mark.django_db


def _approved_grade(team: Team, feedback: str) -> InjectScore:
    reviewer = User.objects.create(username=f"rev{team.team_number}")
    grade = InjectScore.objects.create(
        team=team,
        inject_id="1",
        inject_name="Firewall policy (100 pts)",
        points_awarded=Decimal("90"),
        graded_by=reviewer,
        feedback=feedback,
        notes="",  # the grading flow never populates notes
    )
    grade.approve(reviewer)  # score approved (is_approved=True)
    grade.feedback_approved = True
    grade.save(update_fields=["feedback_approved"])
    return grade


def test_review_page_lists_grades_with_feedback(gold_team_user):
    team = Team.objects.create(team_number=1, team_name="T1", is_active=True)
    _approved_grade(team, "Great, detailed write-up.")

    client = Client()
    client.force_login(gold_team_user)
    resp = client.get(reverse("scoring:review_inject_feedback"))

    assert resp.status_code == 200
    # Shown by default even though feedback is already approved and notes is empty.
    assert "Great, detailed write-up." in resp.content.decode()
