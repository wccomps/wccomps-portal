"""A finding's own Delete button, in a row of the findings table, deletes that finding."""

from decimal import Decimal

import pytest
from django.urls import reverse

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_delete_in_a_row_deletes_that_finding(live_server, pw_browser):
    from scoring.models import RedTeamScore

    user = _create_role_user("red_team", None)
    finding = RedTeamScore.objects.create(attack_vector="ssh", points_per_team=Decimal(10), submitted_by=user)

    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.goto(f"{live_server.url}{reverse('scoring:red_team_scores')}")
        with page.expect_navigation():
            page.get_by_role("button", name="Delete").click()
        assert not RedTeamScore.objects.filter(pk=finding.pk).exists()
    finally:
        context.close()


def test_approve_selected_approves_the_checked_findings(live_server, pw_browser):
    from scoring.models import RedTeamScore

    user = _create_role_user("gold_team", None)
    checked, unchecked = (
        RedTeamScore.objects.create(attack_vector=vector, points_per_team=Decimal(10), submitted_by=user)
        for vector in ("ssh", "rdp")
    )

    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    try:
        page.goto(f"{live_server.url}{reverse('scoring:red_team_findings')}")
        page.locator(f'input[name="finding_ids"][value="{checked.pk}"]').check()
        with page.expect_navigation():
            page.get_by_role("button", name="Approve Selected").click()
        assert list(RedTeamScore.objects.filter(is_approved=True).values_list("pk", flat=True)) == [checked.pk]
        assert not RedTeamScore.objects.get(pk=unchecked.pk).is_approved
    finally:
        context.close()
