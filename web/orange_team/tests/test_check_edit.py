"""Editing a check's rubric keeps grading done on the criteria that stay."""

import json
import re

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.test import Client

from orange_team.forms import extract_criteria
from orange_team.models import OrangeAssignment, OrangeAssignmentResult, OrangeCheck, OrangeCheckCriterion
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def graded_check(gold_team_user: User) -> tuple[OrangeCheck, OrangeCheckCriterion, OrangeCheckCriterion]:
    check = OrangeCheck.objects.create(title="Phones", description="", created_by=gold_team_user)
    keep = OrangeCheckCriterion.objects.create(orange_check=check, label="Polite", points=5, sort_order=0)
    drop = OrangeCheckCriterion.objects.create(orange_check=check, label="Fast", points=3, sort_order=1)
    team = Team.objects.create(team_number=4, team_name="Team 04")
    assignment = OrangeAssignment.objects.create(orange_check=check, user=gold_team_user, team=team)
    OrangeAssignmentResult.objects.create(assignment=assignment, criterion=keep, met=True)
    OrangeAssignmentResult.objects.create(assignment=assignment, criterion=drop, met=True)
    return check, keep, drop


def test_edit_keeps_results_adds_new_and_drops_removed(gold_team_user, graded_check):
    check, keep, drop = graded_check
    client = Client()
    client.force_login(gold_team_user)

    # Row 0 was removed in the browser, so the kept row is still numbered 1.
    response = client.post(
        f"/orange-team/checks/{check.pk}/edit/",
        {
            "title": "Phones (typo fixed)",
            "criterion_id_1": keep.pk,
            "criterion_label_1": "Polite and calm",
            "criterion_points_1": "6",
            "criterion_id_2": "",
            "criterion_label_2": "Escalates",
            "criterion_points_2": "2",
        },
    )

    assert response.status_code == 302
    keep.refresh_from_db()
    assert (keep.label, keep.points, keep.sort_order) == ("Polite and calm", 6, 0)
    assert not OrangeCheckCriterion.objects.filter(pk=drop.pk).exists()
    assignment = OrangeAssignment.objects.get(orange_check=check)
    results = {r.criterion.label: r.met for r in assignment.results.select_related("criterion")}
    assert results == {"Polite and calm": True, "Escalates": False}


def test_edit_recomputes_submitted_and_approved_scores(gold_team_user, graded_check):
    from scoring.models import OrangeTeamScore

    from orange_team.services import create_orange_score_from_assignment

    check, keep, drop = graded_check
    assignment = OrangeAssignment.objects.get(orange_check=check)
    assignment.status = "approved"
    assignment.score = assignment.calculate_score()
    assignment.save()
    score = create_orange_score_from_assignment(assignment, gold_team_user)
    assert (assignment.score, int(score.points_awarded)) == (8, 8)

    team2 = Team.objects.create(team_number=5, team_name="Team 05")
    sub = OrangeAssignment.objects.create(orange_check=check, user=gold_team_user, team=team2, status="submitted")
    OrangeAssignmentResult.objects.create(assignment=sub, criterion=keep, met=True)
    OrangeAssignmentResult.objects.create(assignment=sub, criterion=drop, met=False)
    sub.score = sub.calculate_score()
    sub.save()
    assert sub.score == 5

    client = Client()
    client.force_login(gold_team_user)
    response = client.post(
        f"/orange-team/checks/{check.pk}/edit/",
        {
            "title": "Phones (updated)",
            "criterion_id_0": keep.pk,
            "criterion_label_0": "Polite",
            "criterion_points_0": "10",
        },
    )
    assert response.status_code == 302

    assignment.refresh_from_db()
    sub.refresh_from_db()
    score.refresh_from_db()
    assert assignment.score == 10
    assert sub.score == 10
    assert int(score.points_awarded) == 10
    assert score.description == "Check: Phones (updated)"
    assert OrangeTeamScore.objects.filter(orange_check=check, team=assignment.team).count() == 1


def test_extract_criteria_reads_rows_past_gaps():
    post = QueryDict(mutable=True)
    post.update(
        {"criterion_label_2": "B", "criterion_points_2": "3", "criterion_label_0": "A", "criterion_points_0": "1"}
    )
    post.update({"criterion_label_5": "", "criterion_points_5": "", "criterion_id_5": ""})  # an empty row

    assert extract_criteria(post) == [
        {"id": None, "label": "A", "points": 1, "sort_order": 0},
        {"id": None, "label": "B", "points": 3, "sort_order": 1},
    ]


def _results(check: OrangeCheck) -> dict[str, bool]:
    assignment = OrangeAssignment.objects.get(orange_check=check)
    return {r.criterion.label: r.met for r in assignment.results.select_related("criterion")}


def test_an_out_of_range_criterion_refuses_the_save_and_keeps_the_rubric(gold_team_user, graded_check):
    """A lead typing 30000 for 3000 must get an error, not lose the criterion and its grading."""
    check, keep, drop = graded_check
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(
        f"/orange-team/checks/{check.pk}/edit/",
        {
            "title": "Phones",
            "criterion_id_0": keep.pk,
            "criterion_label_0": "Polite",
            "criterion_points_0": "5",
            "criterion_id_1": drop.pk,
            "criterion_label_1": "Fast",
            "criterion_points_1": "30000",
        },
    )

    assert response.status_code == 200
    assert "Criterion 2 (Fast)" in response.content.decode()
    drop.refresh_from_db()
    assert drop.points == 3
    assert _results(check) == {"Polite": True, "Fast": True}


def _criteria_shown(response) -> list[dict[str, object]]:
    match = re.search(r'<script id="criteria-data" type="application/json">(.*?)</script>', response.content.decode())
    assert match, "the page shows no criteria"
    return json.loads(match.group(1))


def test_a_refused_edit_shows_what_was_posted_and_the_corrected_save_keeps_the_grading(gold_team_user, graded_check):
    """The error page must carry the posted rubric and fields, so saving it again doesn't post an empty rubric."""
    check, keep, drop = graded_check
    client = Client()
    client.force_login(gold_team_user)
    rubric = {
        "criterion_id_0": keep.pk,
        "criterion_label_0": "Polite",
        "criterion_points_0": "5",
        "criterion_id_1": drop.pk,
        "criterion_label_1": "Fast",
        "criterion_points_1": "3",
    }

    refused = client.post(
        f"/orange-team/checks/{check.pk}/edit/",
        {"title": "Phones v2", "max_points": "20000", "time_limit_minutes": "45", **rubric},
    )

    assert refused.status_code == 200
    page = refused.content.decode()
    assert 'value="Phones v2"' in page and 'value="45"' in page
    shown = _criteria_shown(refused)
    assert [(c["id"], c["label"], str(c["points"])) for c in shown] == [
        (str(keep.pk), "Polite", "5"),
        (str(drop.pk), "Fast", "3"),
    ]

    # The browser posts back the rows the page shows, numbered as the page's script numbers them
    reposted = {"title": "Phones v2", "max_points": "200", "time_limit_minutes": "45"}
    for n, c in enumerate(shown):
        reposted |= {
            f"criterion_id_{n}": c["id"],
            f"criterion_label_{n}": c["label"],
            f"criterion_points_{n}": c["points"],
        }
    saved = client.post(f"/orange-team/checks/{check.pk}/edit/", reposted)

    assert saved.status_code == 302
    assert _results(check) == {"Polite": True, "Fast": True}


@pytest.mark.parametrize(
    "row",
    [
        {"criterion_label_5": "bad", "criterion_points_5": "-1"},
        {"criterion_label_5": "", "criterion_points_5": "3"},
        {"criterion_label_5": "x" * 201, "criterion_points_5": "3"},
        {"criterion_label_5": "ok", "criterion_points_5": "3", "criterion_id_5": "two"},
    ],
)
def test_extract_criteria_refuses_a_row_it_cannot_keep(row):
    post = QueryDict(mutable=True)
    post.update({"criterion_label_0": "A", "criterion_points_0": "1", **row})

    with pytest.raises(ValidationError) as refused:
        extract_criteria(post)
    assert refused.value.messages[0].startswith("Criterion 2 (")


@pytest.mark.parametrize("key", ["criterion_label_" + "9" * 5000, "criterion_label_٣", "criterion_name_1"])
def test_extract_criteria_refuses_a_row_key_it_does_not_know(key):
    """Row numbers are ASCII digits the page makes; any other criterion field is refused, not dropped or a 500."""
    post = QueryDict(mutable=True)
    post.update({"criterion_label_0": "A", "criterion_points_0": "1", key: "B"})

    with pytest.raises(ValidationError):
        extract_criteria(post)
