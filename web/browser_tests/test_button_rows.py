"""Button rows: a link button matches the button beside it, and only a boxed row draws a grey bar."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

BACKGROUND = "e => getComputedStyle(e).backgroundColor"


def test_cancel_matches_save_and_the_row_has_no_box(live_server, pw_browser):
    from django.urls import reverse

    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    try:
        page.goto(live_server.url + reverse("scoring:scoring_config"))
        cancel = page.get_by_role("button", name="Cancel")
        save = page.get_by_role("button", name="Save Scoring Setup")
        cancel_box, save_box = cancel.bounding_box(), save.bounding_box()
        assert abs(cancel_box["height"] - save_box["height"]) <= 2
        assert abs(cancel_box["y"] - save_box["y"]) <= 2
        assert cancel.evaluate("e => getComputedStyle(e).textDecorationLine") == "none"
        row = page.locator(".submit-row", has=save)
        assert row.evaluate(BACKGROUND) == "rgba(0, 0, 0, 0)"
    finally:
        context.close()


def test_the_team_pages_actions_row_is_boxed(live_server, pw_browser):
    from django.urls import reverse

    from team.models import Team

    Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})
    context = create_session_context(pw_browser, live_server, role_user("admin"))
    page = context.new_page()
    try:
        page.goto(live_server.url + reverse("admin_team_detail", args=[1]))
        row = page.locator(".submit-row", has=page.get_by_role("button", name="Reset Team"))
        assert row.evaluate(BACKGROUND) != "rgba(0, 0, 0, 0)"
        assert float(row.evaluate("e => parseFloat(getComputedStyle(e).paddingTop)")) > 0
    finally:
        context.close()
