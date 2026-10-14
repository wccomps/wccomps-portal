"""The admin delete page's Yes and No buttons render the same height, side by side."""

import pytest

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_delete_confirmation_buttons_match(live_server, pw_browser):
    from django.contrib.auth.models import User
    from registration.models import Season

    from core.models import UserGroups

    admin = User.objects.create(username="admindelete", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    season = Season.objects.create(name="Spare", year=2090)
    context = create_session_context(pw_browser, live_server, admin)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/admin/registration/season/{season.pk}/delete/")
        yes = page.locator('.delete-confirmation form input[type="submit"]').bounding_box()
        no = page.locator(".delete-confirmation form .cancel-link").bounding_box()
        assert abs(yes["height"] - no["height"]) <= 2
        assert abs(yes["y"] - no["y"]) <= 2
    finally:
        context.close()


def test_changelist_action_go_button_fits_its_label(live_server, pw_browser):
    from django.contrib.auth.models import User
    from registration.models import Season

    from core.models import UserGroups

    admin = User.objects.create(username="admingo", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    Season.objects.create(name="Spare", year=2092)
    context = create_session_context(pw_browser, live_server, admin)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/admin/registration/season/")
        go = page.locator(".actions .button").first
        sizes = go.evaluate(
            "e => ({height: e.clientHeight, content: e.scrollHeight, font: parseFloat(getComputedStyle(e).fontSize),"
            " padding: parseFloat(getComputedStyle(e).paddingTop) + parseFloat(getComputedStyle(e).paddingBottom)})"
        )
        assert sizes["content"] <= sizes["height"]
        assert sizes["height"] - sizes["padding"] >= sizes["font"]
    finally:
        context.close()


def test_ticket_bulk_action_buttons_fit_their_labels(live_server, pw_browser):
    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(f"{live_server.url}/tickets/")
        bar = page.locator("#ticket-list-content .actions")
        bar.evaluate("e => { e.removeAttribute('x-cloak'); e.style.display = 'block'; }")
        buttons = bar.locator(".button")
        assert buttons.count() > 0
        for i in range(buttons.count()):
            sizes = buttons.nth(i).evaluate(
                "e => ({height: e.clientHeight, content: e.scrollHeight,"
                " font: parseFloat(getComputedStyle(e).fontSize),"
                " padding: parseFloat(getComputedStyle(e).paddingTop) + parseFloat(getComputedStyle(e).paddingBottom)})"
            )
            assert sizes["content"] <= sizes["height"], sizes
            assert sizes["height"] - sizes["padding"] >= sizes["font"], sizes
    finally:
        context.close()
