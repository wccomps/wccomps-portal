"""Saving inject grades: only changed points are written, a regrade loses its approval, bad input saves nothing."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from quotient.client import Inject

from scoring.models import InjectScore
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def grading(gold_team_user):
    inject = Inject(inject_id=1, title="Firewall policy", description="")
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n}", is_active=True) for n in (1, 2)]
    reviewer = User.objects.create(username="reviewer")
    first_grader = User.objects.create(username="first-grader")
    for team, points in zip(teams, ("10", "7"), strict=True):
        grade = InjectScore.objects.create(
            team=team, inject_id="1", inject_name=inject.title, points_awarded=Decimal(points), graded_by=first_grader
        )
        grade.approve(reviewer)
    client = Client()
    client.force_login(gold_team_user)
    with patch("quotient.client.QuotientClient") as quotient:
        quotient.return_value.get_injects.return_value = [inject]
        yield client


def _save(client, **points):
    data = {"inject_id": "1", **{f"points_team_{n}": v for n, v in points.items()}}
    return client.post(reverse("scoring:inject_grading") + "?inject=1", data, follow=True)


def test_a_changed_grade_goes_back_for_review_and_an_unchanged_one_is_left_alone(grading, gold_team_user):
    _save(grading, **{"1": "99", "2": "7"})

    changed = InjectScore.objects.get(team__team_number=1)
    unchanged = InjectScore.objects.get(team__team_number=2)
    assert changed.points_awarded == Decimal("99")
    assert (changed.is_approved, changed.approved_by, changed.approved_at) == (False, None, None)
    assert changed.graded_by == gold_team_user
    assert unchanged.is_approved and unchanged.approved_by.username == "reviewer"
    assert unchanged.graded_by.username == "first-grader"


@pytest.mark.parametrize("bad", ["-5", "123456789012"])
def test_out_of_range_points_save_nothing_and_say_why(grading, bad):
    response = _save(grading, **{"1": "3", "2": bad})

    assert response.status_code == 200
    assert "Nothing saved" in response.content.decode()
    assert InjectScore.objects.get(team__team_number=1).points_awarded == Decimal("10")
    assert InjectScore.objects.get(team__team_number=2).points_awarded == Decimal("7")


def _save_from_page(client, loaded, posted):
    """Post the grading form as a browser does: each team's field plus the value it loaded with."""
    data = {"inject_id": "1"}
    for n in posted:
        data[f"points_team_{n}"] = posted[n]
        data[f"loaded_team_{n}"] = loaded.get(n, "")
    return client.post(reverse("scoring:inject_grading") + "?inject=1", data, follow=True)


def test_a_stale_page_leaves_a_newer_grade_alone(grading):
    """Grader A loaded team 2 = 7; grader B has since saved 15; A changes only team 1."""
    InjectScore.objects.filter(team__team_number=2).update(points_awarded=Decimal("15"))

    _save_from_page(grading, loaded={"1": "10", "2": "7"}, posted={"1": "12", "2": "7"})

    assert InjectScore.objects.get(team__team_number=1).points_awarded == Decimal("12")
    assert InjectScore.objects.get(team__team_number=2).points_awarded == Decimal("15")


def test_a_field_the_grader_changed_is_saved_even_if_someone_else_changed_it_too(grading):
    InjectScore.objects.filter(team__team_number=2).update(points_awarded=Decimal("15"))

    _save_from_page(grading, loaded={"1": "10", "2": "7"}, posted={"1": "10", "2": "9"})

    assert InjectScore.objects.get(team__team_number=2).points_awarded == Decimal("9")


def test_a_first_grade_saved_by_someone_else_meanwhile_is_updated_not_a_500(grading):
    """Two graders saving a team's first grade: the second's lookup missed a row the first just created."""
    from scoring.views import injects

    team = Team.objects.create(team_number=3, team_name="Team 3", is_active=True)
    InjectScore.objects.create(team=team, inject_id="1", inject_name="Firewall policy", points_awarded=Decimal("4"))
    real_select_for_update = InjectScore.objects.select_for_update

    class LookupMissesTeam3:
        def filter(self, **kwargs):
            return [g for g in real_select_for_update().filter(**kwargs) if g.team_id != team.id]

        def get_or_create(self, **kwargs):
            return real_select_for_update().get_or_create(**kwargs)

    with patch.object(injects.InjectScore.objects, "select_for_update", return_value=LookupMissesTeam3()):
        response = _save_from_page(grading, loaded={"3": ""}, posted={"3": "6"})

    assert response.status_code == 200
    assert InjectScore.objects.get(team=team).points_awarded == Decimal("6")
