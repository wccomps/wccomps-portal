"""A finding's Gold Team review page and its Red Team page each keep their own nav, links and actions."""

import re
from collections.abc import Callable
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from core.tests.roles import role_user
from scoring.models import RedTeamScore

pytestmark = pytest.mark.django_db

RED_TEAM_ONLY = ["scoring:submit_red_score", "scoring:red_team_scores", "scoring:ip_pool_list"]


def _get(user: User, url: str) -> str:
    client = Client()
    client.force_login(user)
    response = client.get(url)
    assert response.status_code == 200
    return response.content.decode()


def _review(user: User, finding: RedTeamScore) -> str:
    return _get(user, reverse("scoring:review_red_finding", args=[finding.pk]))


def _red(user: User, finding: RedTeamScore) -> str:
    return _get(user, reverse("scoring:view_red_score", args=[finding.pk]))


def _targets(html: str) -> set[str]:
    return set(re.findall(r'(?:href|action)="([^"]+)"', html))


def _current_hrefs(html: str) -> set[str]:
    tags = [tag for tag in re.findall(r"<a\b[^>]*>", html) if 'aria-current="page"' in tag]
    return {m.group(1) for tag in tags if (m := re.search(r'href="([^"]+)"', tag))}


@pytest.fixture
def gold_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("gold_reviewer", ["WCComps_GoldTeam"])


@pytest.fixture
def finding(red_team_user: User) -> RedTeamScore:
    return RedTeamScore.objects.create(points_per_team=Decimal(10), submitted_by=red_team_user)


def test_a_gold_reviewer_gets_no_link_to_a_red_team_only_page(gold_user: User, finding: RedTeamScore) -> None:
    targets = _targets(_review(gold_user, finding))
    assert not targets & {reverse(name) for name in RED_TEAM_ONLY}
    assert reverse("scoring:delete_red_score", args=[finding.pk]) not in targets


def test_a_gold_reviewer_goes_back_to_the_review_list(gold_user: User, finding: RedTeamScore) -> None:
    assert reverse("scoring:red_team_findings") in _targets(_review(gold_user, finding))


def test_the_review_page_marks_scoring_review_and_its_red_team_tab_current(
    gold_user: User, finding: RedTeamScore
) -> None:
    assert _current_hrefs(_review(gold_user, finding)) == {
        reverse("scoring:review_incidents"),
        reverse("scoring:red_team_findings"),
    }


def test_a_gold_reviewer_can_approve_a_pending_finding_from_its_page(gold_user: User, finding: RedTeamScore) -> None:
    html = _review(gold_user, finding)
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
    assert reverse("scoring:bulk_approve_red_scores") not in _targets(_review(gold_user, finding))


def test_the_review_page_is_gold_team_only(red_team_user: User, finding: RedTeamScore) -> None:
    client = Client()
    client.force_login(red_team_user)
    assert client.get(reverse("scoring:review_red_finding", args=[finding.pk])).status_code == 403


def test_a_gold_reviewer_on_the_red_team_page_is_sent_to_the_review_page(
    gold_user: User, finding: RedTeamScore
) -> None:
    client = Client()
    client.force_login(gold_user)
    response = client.get(reverse("scoring:view_red_score", args=[finding.pk]))
    assert response.status_code == 302
    assert response["Location"] == reverse("scoring:review_red_finding", args=[finding.pk])


def test_the_review_list_links_each_finding_to_its_review_page(gold_user: User, finding: RedTeamScore) -> None:
    targets = _targets(_get(gold_user, reverse("scoring:red_team_findings")))
    assert reverse("scoring:review_red_finding", args=[finding.pk]) in targets
    assert reverse("scoring:view_red_score", args=[finding.pk]) not in targets


def test_the_red_team_list_links_each_finding_to_its_red_team_page(red_team_user: User, finding: RedTeamScore) -> None:
    targets = _targets(_get(red_team_user, reverse("scoring:red_team_scores")))
    assert reverse("scoring:view_red_score", args=[finding.pk]) in targets
    assert reverse("scoring:review_red_finding", args=[finding.pk]) not in targets


def test_a_red_team_member_reading_the_review_list_gets_the_red_team_pages(
    red_team_user: User, finding: RedTeamScore
) -> None:
    """The review list admits the Red Team to read; the review page is Gold Team's, so their rows open the Red Team
    page instead of a 403."""
    html = _get(red_team_user, reverse("scoring:red_team_findings"))
    targets = _targets(html)
    assert reverse("scoring:view_red_score", args=[finding.pk]) in targets
    assert reverse("scoring:review_red_finding", args=[finding.pk]) not in targets
    assert "Approve Selected" not in html


def test_a_red_team_member_keeps_the_red_team_nav_and_no_approve(red_team_user: User, finding: RedTeamScore) -> None:
    html = _red(red_team_user, finding)
    targets = _targets(html)
    assert {reverse(name) for name in RED_TEAM_ONLY} <= targets
    assert reverse("scoring:submit_red_score") in targets
    assert reverse("scoring:bulk_approve_red_scores") not in targets
    assert reverse("scoring:red_team_scores") in _current_hrefs(html)


def test_only_the_owner_can_delete_and_only_while_pending(red_team_user: User, finding: RedTeamScore) -> None:
    delete = reverse("scoring:delete_red_score", args=[finding.pk])
    other = role_user("red_team", username="other_red")
    assert delete in _targets(_red(red_team_user, finding))
    assert delete not in _targets(_red(other, finding))
    finding.approve(other)
    assert delete not in _targets(_red(red_team_user, finding))


def test_both_pages_show_the_status_in_the_modules_toolbar(
    gold_user: User, red_team_user: User, finding: RedTeamScore
) -> None:
    for html in (_review(gold_user, finding), _red(red_team_user, finding)):
        toolbar = re.search(r'<div class="toolbar">(.*?)</div>', html, re.S)
        assert toolbar and "Pending Review" in toolbar.group(1)
