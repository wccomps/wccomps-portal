"""The scorecard shows each inject grade as points earned over points available."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

from scoring.models import InjectScore
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("template", ["scoring/_scorecard_body.html", "scoring/scorecard_print.html"])
def test_scorecard_renders_inject_points_over_maximum(template: str) -> None:
    from scoring.views.leaderboard import _scorecard_context

    team = Team.objects.create(team_number=7, team_name="Team G", is_active=True)
    InjectScore.objects.create(
        team=team,
        inject_id="memo",
        inject_name="Executive memo",
        max_points=Decimal("100"),
        points_awarded=Decimal("75"),
        is_approved=True,
    )

    page = render_to_string(template, _scorecard_context(7))

    assert "Executive memo" in page
    assert "75 / 100" in page


def test_scorecard_keeps_legacy_inject_without_maximum(gold_team_user: User) -> None:
    team = Team.objects.create(team_number=7, team_name="Team G", is_active=True)
    InjectScore.objects.create(
        team=team,
        inject_id="legacy",
        inject_name="Legacy inject",
        max_points=None,
        points_awarded=Decimal("25"),
        is_approved=True,
    )

    client = Client()
    client.force_login(gold_team_user)
    response = client.get(reverse("scoring:scorecard", args=[7]))

    assert response.status_code == 200
    assert b"Legacy inject" in response.content
    assert b"25 /" not in response.content
