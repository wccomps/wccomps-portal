from collections.abc import Callable

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


def test_submit_persists_posted_score_without_prior_save(setup: tuple[Client, OrangeAssignment]) -> None:
    # Typing a score and clicking Submit (without a separate autosave) must persist it.
    c, a = setup
    resp = c.post(f"/orange-team/assignments/{a.id}/submit/", {"score": "16"})
    assert resp.status_code == 302
    a.refresh_from_db()
    assert a.status == "submitted"
    assert a.score == 16


def test_submit_rejects_posted_score_over_max(setup: tuple[Client, OrangeAssignment]) -> None:
    c, a = setup
    c.post(f"/orange-team/assignments/{a.id}/submit/", {"score": "999"})
    a.refresh_from_db()
    assert a.status != "submitted"
    assert a.score is None


def test_submit_without_posted_score_keeps_existing(setup: tuple[Client, OrangeAssignment]) -> None:
    # A criteria-free submit with no score posted keeps the stored score (not recomputed to 0).
    c, a = setup
    a.score = 14
    a.save()
    resp = c.post(f"/orange-team/assignments/{a.id}/submit/")
    assert resp.status_code == 302
    a.refresh_from_db()
    assert a.status == "submitted"
    assert a.score == 14
