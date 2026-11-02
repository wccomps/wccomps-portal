"""One rule for an orange check's maximum, read from one annotation on the pages that list checks."""

from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from orange_team.models import OrangeAssignment, OrangeCheck, OrangeCheckCriterion, check_worth
from team.models import Team

pytestmark = pytest.mark.django_db


def test_a_checks_maximum_is_its_criteria_total_else_its_max_points() -> None:
    plain = OrangeCheck.objects.create(title="Plain", description="", max_points=40)
    rubric = OrangeCheck.objects.create(title="Rubric", description="", max_points=99)
    OrangeCheckCriterion.objects.create(orange_check=rubric, label="A", points=5)
    OrangeCheckCriterion.objects.create(orange_check=rubric, label="B", points=7)
    annotated = {c.title: c.max_score for c in OrangeCheck.objects.with_max_score()}
    assert annotated == {"Plain": 40, "Rubric": 12}
    assert (plain.max_score, rubric.max_score) == (40, 12)
    assert OrangeCheck.objects.total_max() == 52


def test_a_check_is_worth_its_criteria_else_its_max_points() -> None:
    assert (check_worth([], 7), check_worth([2, 3], 7)) == (7, 5)


def _queries(client: Client, url: str) -> int:
    with CaptureQueriesContext(connection) as queries:
        assert client.get(url).status_code == 200
    return len(queries)


def _add_assignments(user: User, count: int, status: str) -> None:
    start = OrangeAssignment.objects.count()
    for n in range(start, start + count):
        check = OrangeCheck.objects.create(title=f"C{n}", description="", max_points=10)
        OrangeCheckCriterion.objects.create(orange_check=check, label="A", points=3)
        team = Team.objects.create(team_number=n + 1, team_name=f"T{n + 1}")
        OrangeAssignment.objects.create(orange_check=check, user=user, team=team, status=status, score=1)


@pytest.mark.parametrize(
    ("role", "url", "status"),
    [
        ("WCComps_OrangeTeam", "orange_team:dashboard", "pending"),
        ("WCComps_OrangeTeam_Lead", "orange_team:review_queue", "submitted"),
        ("WCComps_OrangeTeam_Lead", "orange_team:check_list", "pending"),
    ],
)
def test_a_list_of_checks_reads_their_maximums_without_a_query_each(
    create_user_with_groups: Callable[..., User], role: str, url: str, status: str
) -> None:
    user = create_user_with_groups("orange", [role])
    client = Client()
    client.force_login(user)
    _add_assignments(user, 1, status)
    one = _queries(client, reverse(url))
    _add_assignments(user, 3, status)
    assert _queries(client, reverse(url)) == one
