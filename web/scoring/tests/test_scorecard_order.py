"""Scorecard lists break ties by pk, so a frozen scorecard and a later one list equal rows in the same order."""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from registration.services import set_team_school

from scoring.calculator import compute_standings
from scoring.models import InjectScore, OrangeTeamScore, ServiceDetail, ServiceScore
from scoring.scorecard import build_scorecard_context
from team.models import Team

pytestmark = pytest.mark.django_db


def _team_with_school(number=1):
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
    set_team_school(team, f"School {number}", contact_email="c@example.edu")
    ServiceScore.objects.create(team=team, service_points=Decimal(100))
    return team


def test_orange_scorecard_list_breaks_ties_by_pk():
    team = _team_with_school()
    OrangeTeamScore.objects.create(team=team, description="Same", points_awarded=Decimal(5), is_approved=True)
    standings = compute_standings()

    context = build_scorecard_context(standings[0], standings)

    assert context["orange_scores"].query.order_by == ("description", "pk")


def test_inject_scorecard_list_breaks_ties_by_pk():
    team = _team_with_school()
    InjectScore.objects.create(
        team=team, inject_id="a", inject_name="Same", points_awarded=Decimal(5), is_approved=True
    )
    standings = compute_standings()

    with CaptureQueriesContext(connection) as queries:
        build_scorecard_context(standings[0], standings)

    listing = next(q["sql"] for q in queries if q["sql"].startswith('SELECT "inject_grade"."id"'))
    assert listing.endswith('ORDER BY "inject_grade"."inject_name" ASC, "inject_grade"."id" ASC')


def test_service_scorecard_list_breaks_ties_by_pk():
    team = _team_with_school()
    ServiceDetail.objects.create(team=team, service_name="Same", points=Decimal(5))
    standings = compute_standings()

    with CaptureQueriesContext(connection) as queries:
        build_scorecard_context(standings[0], standings)

    listing = next(q["sql"] for q in queries if q["sql"].startswith('SELECT "service_detail"."id"'))
    assert listing.endswith('ORDER BY "service_detail"."service_name" ASC, "service_detail"."id" ASC')
