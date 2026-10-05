"""Saving inject grades backfills each grade's max_points and derives the inject_max."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from scoring.models import InjectScore, ScoringTemplate
from team.models import Team

pytestmark = pytest.mark.django_db


def test_saving_grades_sets_max_points_and_inject_max(gold_team_user, django_capture_on_commit_callbacks):
    inject = MagicMock(inject_id=1, title="Recon report (150 pts)", description="")
    Team.objects.create(team_number=1, team_name="T1", is_active=True)
    ScoringTemplate.objects.create(
        service_weight=Decimal("40"),
        inject_weight=Decimal("40"),
        orange_weight=Decimal("20"),
        inject_max=Decimal("1"),
    )
    client = Client()
    client.force_login(gold_team_user)

    with (
        patch("quotient.client.QuotientClient") as quotient,
        django_capture_on_commit_callbacks(execute=True),
    ):
        quotient.return_value.get_injects.return_value = [inject]
        client.post(
            reverse("scoring:inject_grading") + "?inject=1",
            {"inject_id": "1", "points_team_1": "120"},
            follow=True,
        )

    grade = InjectScore.objects.get(team__team_number=1)
    assert grade.points_awarded == Decimal("120")
    assert grade.max_points == Decimal("150")  # backfilled from the title
    assert ScoringTemplate.objects.first().inject_max == Decimal("150")  # derived
