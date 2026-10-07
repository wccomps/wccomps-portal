"""Standings are computed from the scoring inputs on every read; exclusions live on their own rows."""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from scoring.calculator import compute_standings, get_leaderboard
from scoring.models import OrangeTeamScore, ScoringExclusion, ScoringTemplate, ServiceScore
from team.models import Team


@pytest.fixture
def unit_modifiers(db):
    # Maxes proportional to weights -> every derived modifier is exactly 1 (raw == scaled).
    return ScoringTemplate.objects.create(service_max=Decimal("40"), inject_max=Decimal("40"), orange_max=Decimal("20"))


def _team(number, service, is_active=True):
    team = Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=is_active)
    ServiceScore.objects.create(team=team, service_points=Decimal(service))
    return team


@pytest.mark.django_db
def test_leaderboard_reflects_an_approval_without_recalculating(unit_modifiers, gold_team_user):
    team = _team(1, "100")
    client = Client()
    client.force_login(gold_team_user)

    before = client.get(reverse("scoring:api_scores")).json()["scores"]
    OrangeTeamScore.objects.create(team=team, description="x", points_awarded=Decimal("25")).approve(gold_team_user)
    after = client.get(reverse("scoring:api_scores")).json()["scores"]

    assert [s["total"] for s in before] == [100.0]
    assert [s["total"] for s in after] == [125.0]


@pytest.mark.django_db
def test_excluded_team_is_unranked_and_other_ranks_stay_contiguous(unit_modifiers):
    first, middle, last = _team(1, "300"), _team(2, "200"), _team(3, "100")
    ScoringExclusion.objects.create(team=middle)

    ranks = {s.team: s.rank for s in compute_standings()}

    assert ranks == {first: 1, middle: None, last: 2}
    assert [s.team for s in get_leaderboard()] == [first, last]


@pytest.mark.django_db
def test_inactive_and_idle_teams_are_not_ranked(unit_modifiers):
    scored = _team(1, "100")
    _team(2, "500", is_active=False)
    idle = Team.objects.create(team_number=3, team_name="Team 3", is_active=True)

    standings = compute_standings()

    assert [(s.team, s.rank) for s in standings] == [(scored, 1), (idle, None)]


@pytest.mark.django_db
def test_standing_school_name(unit_modifiers):
    from team.models import SchoolInfo

    team1 = _team(1, "100")
    SchoolInfo.objects.create(team=team1, school_name="University of Testing", contact_email="u@test.edu")
    _team(2, "50")

    standings = compute_standings()
    s1 = next(s for s in standings if s.team == team1)
    s2 = next(s for s in standings if s.team != team1)

    assert s1.school_name == "University of Testing"
    assert s2.school_name == ""

