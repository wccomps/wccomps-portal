"""Select-all on a bulk-review table checks (and clears) every row — shared bulkSelectMixin."""

from decimal import Decimal

import pytest

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_select_all_checks_and_clears_every_row(live_server, pw_browser):
    from scoring.models import OrangeTeamScore

    from team.models import Team

    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    for i in range(3):
        OrangeTeamScore.objects.create(
            team=team, description=f"check {i}", points_awarded=Decimal("5"), is_approved=False
        )

    user = _create_role_user("orange_team", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

    try:
        page.goto(f"{live_server.url}/scoring/orange/?status=pending")
        rows = page.locator('tbody input[type="checkbox"][name="adjustment_ids"]')
        select_all = page.locator('thead input[type="checkbox"][title="Select all"]')

        assert rows.count() == 3
        for i in range(3):
            assert not rows.nth(i).is_checked()

        select_all.check()
        for i in range(3):
            assert rows.nth(i).is_checked(), f"row {i} not checked after select-all"

        select_all.uncheck()
        for i in range(3):
            assert not rows.nth(i).is_checked(), f"row {i} still checked after clear-all"

        assert not errors, errors
    finally:
        context.close()


def test_selecting_every_row_checks_the_header(live_server, pw_browser):
    from scoring.models import OrangeTeamScore

    from team.models import Team

    team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    for i in range(2):
        OrangeTeamScore.objects.create(
            team=team, description=f"check {i}", points_awarded=Decimal("5"), is_approved=False
        )

    user = _create_role_user("orange_team", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()

    try:
        page.goto(f"{live_server.url}/scoring/orange/?status=pending")
        rows = page.locator('tbody input[type="checkbox"][name="adjustment_ids"]')
        select_all = page.locator('thead input[type="checkbox"][title="Select all"]')

        rows.nth(0).check()
        assert not select_all.is_checked()  # partial selection
        rows.nth(1).check()
        assert select_all.is_checked()  # every row selected -> header checked
    finally:
        context.close()
