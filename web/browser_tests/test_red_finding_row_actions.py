"""In the findings table, a row's Delete deletes that finding and Approve Selected approves the checked rows; on the
submit page, a team button selects that team."""

from decimal import Decimal

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_delete_in_a_row_deletes_that_finding(live_server, pw_browser):
    from scoring.models import RedTeamScore

    user = role_user("red_team")
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

    user = role_user("gold_team")
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


def test_a_team_button_on_the_submit_page_selects_that_team(live_server, pw_browser):
    from team.models import Team

    user = role_user("red_team")
    for number in (3, 4):
        Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}{reverse('scoring:submit_red_score')}")
        button = page.locator(".team-btn").nth(1)
        button.click()
        checkbox_id = button.get_attribute("data-checkbox-id")
        assert page.locator(f"#{checkbox_id}").is_checked()
        assert "team-btn--selected" in (button.get_attribute("class") or "")
        assert page.locator("#selectedCount").inner_text() == "1"
    finally:
        context.close()
