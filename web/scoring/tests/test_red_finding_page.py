"""A finding's page for each role that may open it: a Gold Team reviewer without the Red Team role gets the scoring
review nav, a way back to the review list and an Approve button on a pending finding, never a link that answers
them 403; a Red Team member keeps the Red Team nav."""

import re
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from scoring.models import RedTeamScore

pytestmark = pytest.mark.django_db

RED_TEAM_ONLY = ["scoring:submit_red_score", "scoring:red_team_scores", "scoring:ip_pool_list"]


def _page(user: User, finding: RedTeamScore) -> str:
    client = Client()
    client.force_login(user)
    response = client.get(reverse("scoring:view_red_score", args=[finding.pk]))
    assert response.status_code == 200
    return response.content.decode()


def _targets(html: str) -> set[str]:
    return set(re.findall(r'(?:href|action)="([^"]+)"', html))


@pytest.fixture
def gold_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("gold_reviewer", ["WCComps_GoldTeam"])


@pytest.fixture
def finding(red_team_user: User) -> RedTeamScore:
    return RedTeamScore.objects.create(points_per_team=Decimal(10), submitted_by=red_team_user)


def test_a_gold_reviewer_gets_no_link_to_a_red_team_only_page(gold_user: User, finding: RedTeamScore) -> None:
    targets = _targets(_page(gold_user, finding))
    assert not targets & {reverse(name) for name in RED_TEAM_ONLY}
    assert reverse("scoring:delete_red_score", args=[finding.pk]) not in targets


def test_a_gold_reviewer_goes_back_to_the_review_list(gold_user: User, finding: RedTeamScore) -> None:
    assert reverse("scoring:red_team_findings") in _targets(_page(gold_user, finding))


def test_a_gold_reviewer_can_approve_a_pending_finding_from_its_page(gold_user: User, finding: RedTeamScore) -> None:
    html = _page(gold_user, finding)
    form = re.search(
        rf'<form[^>]*action="{re.escape(reverse("scoring:bulk_approve_red_scores"))}"[^>]*>(.*?)</form>', html, re.S
    )
    assert form
    assert re.search(rf'name="finding_ids"[^>]*value="{finding.pk}"', form.group(1))

    client = Client()
    client.force_login(gold_user)
    client.post(reverse("scoring:bulk_approve_red_scores"), {"finding_ids": [finding.pk]})
    finding.refresh_from_db()
    assert finding.is_approved


def test_an_approved_finding_has_no_approve_button(gold_user: User, finding: RedTeamScore) -> None:
    finding.approve(gold_user)
    assert reverse("scoring:bulk_approve_red_scores") not in _targets(_page(gold_user, finding))


def test_a_red_team_member_keeps_the_red_team_nav_and_no_approve(red_team_user: User, finding: RedTeamScore) -> None:
    targets = _targets(_page(red_team_user, finding))
    assert {reverse(name) for name in RED_TEAM_ONLY} <= targets
    assert reverse("scoring:bulk_approve_red_scores") not in targets
