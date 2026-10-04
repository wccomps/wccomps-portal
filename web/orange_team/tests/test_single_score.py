import json
from typing import Callable

import pytest
from django.contrib.auth.models import User
from django.test import Client

from orange_team.models import OrangeAssignment, OrangeCheck
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(create_user_with_groups: Callable[..., User]) -> tuple[Client, OrangeAssignment]:
    user = create_user_with_groups("orange1", ["WCComps_OrangeTeam"])
    team = Team.objects.create(team_number=1, team_name="Team 01")
    check = OrangeCheck.objects.create(title="C", description="d", max_points=20)
    assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
    c = Client()
    c.force_login(user)
    return c, assignment


def test_save_single_score(setup: tuple[Client, OrangeAssignment]) -> None:
    c, a = setup
    resp = c.post(
        f"/orange-team/assignments/{a.id}/save/",
        data=json.dumps({"score": 14}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    a.refresh_from_db()
    assert a.score == 14
    assert a.status == "in_progress"


def test_save_single_score_over_max_rejected(setup: tuple[Client, OrangeAssignment]) -> None:
    c, a = setup
    resp = c.post(
        f"/orange-team/assignments/{a.id}/save/",
        data=json.dumps({"score": 99}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    a.refresh_from_db()
    assert a.score is None


def test_submit_keeps_single_score(setup: tuple[Client, OrangeAssignment]) -> None:
    c, a = setup
    c.post(
        f"/orange-team/assignments/{a.id}/save/",
        data=json.dumps({"score": 14}),
        content_type="application/json",
    )
    resp = c.post(f"/orange-team/assignments/{a.id}/submit/")
    assert resp.status_code == 302
    a.refresh_from_db()
    assert a.status == "submitted"
    assert a.score == 14  # not recomputed to 0 from absent criteria
