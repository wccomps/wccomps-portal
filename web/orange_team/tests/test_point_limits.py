"""An orange check's points are limited on the models, so the admin, the lead's pages and the scoring sum agree.

Every check's maximum counts toward the orange maximum, which is limited to MAX_TOTAL_POINTS.
"""

from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client

from orange_team.models import MAX_POINTS, MAX_TOTAL_POINTS, OrangeCheck, OrangeCheckCriterion

pytestmark = pytest.mark.django_db


def test_a_check_or_criterion_over_the_limit_is_refused_wherever_it_is_edited() -> None:
    check = OrangeCheck(title="C", description="d", max_points=MAX_POINTS + 1)
    with pytest.raises(ValidationError):
        check.full_clean()
    check = OrangeCheck.objects.create(title="C", description="d")
    with pytest.raises(ValidationError):
        OrangeCheckCriterion(orange_check=check, label="L", points=MAX_POINTS + 1).full_clean()


@pytest.fixture
def checks_at_the_scoring_limit(db: object) -> None:
    """Checks whose points together reach MAX_TOTAL_POINTS."""
    count, rest = divmod(MAX_TOTAL_POINTS, MAX_POINTS)
    OrangeCheck.objects.bulk_create(
        [OrangeCheck(title=f"C{n}", description="", max_points=MAX_POINTS) for n in range(count)]
        + [OrangeCheck(title="Rest", description="", max_points=rest)]
    )


def test_a_check_that_would_overflow_the_scoring_sum_is_refused(
    checks_at_the_scoring_limit: None, create_user_with_groups: Callable[..., User]
) -> None:
    with pytest.raises(ValidationError):
        OrangeCheck(title="One more", description="", max_points=1).full_clean()

    client = Client()
    client.force_login(create_user_with_groups("orangelead", ["WCComps_OrangeTeam_Lead"]))
    response = client.post("/orange-team/checks/create/", {"title": "One more", "max_points": "1"})

    assert response.status_code == 200
    assert not OrangeCheck.objects.filter(title="One more").exists()
