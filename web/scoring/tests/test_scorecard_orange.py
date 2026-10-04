"""The scorecard shows the per-check orange breakdown (approved checks only)."""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from scoring.models import OrangeTeamScore
from team.models import Team

pytestmark = pytest.mark.django_db


def _orange(team, desc, pts, approved=True):
    return OrangeTeamScore.objects.create(
        team=team, description=desc, points_awarded=Decimal(str(pts)), is_approved=approved
    )


def test_context_lists_only_approved_orange_checks():
    from scoring.views.leaderboard import _scorecard_context

    team = Team.objects.create(team_number=7, team_name="Team G", is_active=True)
    _orange(team, "Keycloak Check (max 10 pts)", 10)
    _orange(team, "Website Check (max 10 pts)", 0)
    _orange(team, "Not yet reviewed (max 20 pts)", 20, approved=False)

    ctx = _scorecard_context(7)

    descs = [o.description for o in ctx["orange_scores"]]
    assert "Keycloak Check (max 10 pts)" in descs
    assert "Website Check (max 10 pts)" in descs
    assert "Not yet reviewed (max 20 pts)" not in descs
    assert ctx["orange_total"] == Decimal("10")


def test_scorecard_page_renders_the_orange_breakdown(gold_team_user):
    team = Team.objects.create(team_number=7, team_name="Team G", is_active=True)
    _orange(team, "Keycloak Check (max 10 pts)", 10)

    client = Client()
    client.force_login(gold_team_user)
    response = client.get(reverse("scoring:scorecard", args=[7]))

    assert response.status_code == 200
    assert b"Orange Team Detail" in response.content
    assert b"Keycloak Check (max 10 pts)" in response.content
