"""In the findings table, a row's Delete deletes that finding and Approve Selected approves the checked rows; on the
submit page, a team button selects that team and Select All and Deselect All pick every team or none."""

from decimal import Decimal

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_delete_in_a_row_deletes_that_finding(live_server, pw_browser):
    from scoring.models import RedTeamScore

    user = role_user("red_team")
    finding = RedTeamScore.objects.create(points_per_team=Decimal(10), submitted_by=user)

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

    user = role_user("gold_team")
    checked, unchecked = (RedTeamScore.objects.create(points_per_team=Decimal(10), submitted_by=user) for _ in range(2))

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


def test_a_team_button_on_the_submit_page_selects_that_team(live_server, pw_browser):
    from team.models import Team

    user = role_user("red_team")
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=True) for n in (3, 4)]
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{reverse('scoring:submit_red_score')}")
        button = page.get_by_role("button", name="Team 4")
        button.click()
        assert button.get_attribute("aria-pressed") == "true"
        assert page.evaluate(
            "Array.from(document.getElementById('id_affected_teams').selectedOptions, o => o.value)"
        ) == [str(teams[1].pk)]
        assert page.locator("#selectedCount").inner_text() == "1"

        page.get_by_role("button", name="Select All", exact=True).click()
        assert page.locator("#selectedCount").inner_text() == "2"
        page.get_by_role("button", name="Deselect All").click()
        assert page.locator("#selectedCount").inner_text() == "0"
        assert button.get_attribute("aria-pressed") == "false"
    finally:
        context.close()
