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


def test_batch_peer_context_queries_injects_and_services_once():
    from scoring.scorecard import build_peer_context

    t1, t2 = _team_with_school(1), _team_with_school(2)
    InjectScore.objects.create(team=t1, inject_id="a", inject_name="Inj A", points_awarded=Decimal(5), is_approved=True)
    InjectScore.objects.create(team=t2, inject_id="a", inject_name="Inj A", points_awarded=Decimal(9), is_approved=True)
    ServiceDetail.objects.create(team=t1, service_name="svc-1", points=Decimal(10))
    ServiceDetail.objects.create(team=t2, service_name="svc-1", points=Decimal(20))
    standings = compute_standings()

    with CaptureQueriesContext(connection) as queries:
        peer_context = build_peer_context(standings)
        for s in standings:
            build_scorecard_context(s, standings, peer_context=peer_context)

    inject_queries = [q["sql"] for q in queries if 'FROM "inject_grade"' in q["sql"]]
    service_queries = [q["sql"] for q in queries if 'FROM "service_detail"' in q["sql"]]
    assert len(inject_queries) == 1
    assert len(service_queries) == 1
