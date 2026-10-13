"""A frozen scorecard renders byte for byte like the live one it was taken from."""

import json
import re
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.template.loader import render_to_string

from scoring.models import (
    AttackType,
    InjectScore,
    OrangeTeamScore,
    RedTeamScore,
    ScoringExclusion,
    ServiceDetail,
    ServiceScore,
)
from scoring.snapshot import context_from_snapshot, serialize_scorecard
from scoring.views.leaderboard import _scorecard_context
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def scored_teams():
    red_user = User.objects.create_user("red")
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=True) for n in (1, 2, 3)]
    for team, points in zip(teams, (300, 200, 100), strict=True):
        ServiceScore.objects.create(
            team=team, service_points=Decimal(points), sla_violations=Decimal(-5), point_adjustments=Decimal(-20)
        )
        ServiceDetail.objects.create(team=team, service_name="web-http", points=Decimal(points), sla_violations=2)
        InjectScore.objects.create(
            team=team,
            inject_id="1",
            inject_name="Memo",
            max_points=Decimal(10),
            points_awarded=Decimal("7.50"),
            is_approved=True,
            feedback="Clear and complete.",
            feedback_approved=True,
        )
        OrangeTeamScore.objects.create(
            team=team, description="Phone check (max 10 pts)", points_awarded=Decimal("8.25"), is_approved=True
        )
    finding = RedTeamScore.objects.create(
        attack_type=AttackType.objects.get_or_create(name="Default Credentials")[0],
        attack_vector="ssh",
        source_ip="10.0.0.5",
        affected_boxes=["web01", "db01"],
        affected_service="ssh",
        root_access=True,
        credentials_recovered=True,
        points_per_team=Decimal("150.00"),
        submitted_by=red_user,
        is_approved=True,
    )
    finding.affected_teams.add(teams[0])
    ScoringExclusion.objects.create(team=teams[2])
    return teams


def _frozen(context):
    """Through JSON, like the JSONField it is stored in."""
    return context_from_snapshot(json.loads(json.dumps(serialize_scorecard(context))))


def _render(template, context):
    """The print template stamps the time ({% now %}); drop it so two renders a minute apart still match."""
    return re.sub(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", "", render_to_string(template, context))


@pytest.mark.parametrize("team_number", [1, 2, 3])
@pytest.mark.parametrize("template", ["scoring/_scorecard_body.html", "scoring/scorecard_print.html"])
def test_snapshot_renders_like_the_live_scorecard(scored_teams, team_number, template):
    live = _scorecard_context(team_number)
    urls = {"pdf_url": "/pdf/", "email_url": "/email/"}

    assert _render(template, {**_frozen(live), **urls}) == _render(template, {**live, **urls})


def test_snapshot_keeps_decimals_exact(scored_teams):
    snapshot = serialize_scorecard(_scorecard_context(1))

    assert snapshot["orange_scores"][0]["points_awarded"] == {"$d": "8.25"}
    assert context_from_snapshot(snapshot)["orange_total"] == Decimal("8.25")
