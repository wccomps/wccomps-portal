"""Django admin pages keep space under the breadcrumb bar; portal pages keep theirs flush."""

import pytest

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_admin_add_button_sits_below_the_breadcrumb_bar(live_server, pw_browser):
    from django.contrib.auth.models import User

    from core.models import UserGroups

    admin = User.objects.create(username="adminspacing", is_staff=True, is_superuser=True)
    UserGroups.objects.update_or_create(user=admin, defaults={"groups": ["WCComps_Discord_Admin"], "authentik_id": "x"})
    context = create_session_context(pw_browser, live_server, admin)
    page = context.new_page()
    try:
        page.goto(live_server.url + "/admin/ticketing/ticket/")
        bar = page.locator(".breadcrumbs").first.bounding_box()
        button = page.locator(".object-tools a").first.bounding_box()
        heading = page.locator("#content h1").bounding_box()
        assert button["y"] - (bar["y"] + bar["height"]) >= 15
        assert heading["y"] - (bar["y"] + bar["height"]) >= 15
    finally:
        context.close()


def test_portal_page_breadcrumbs_stay_flush(live_server, pw_browser):
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    try:
        page.goto(live_server.url + "/ops/admin/competition/")
        assert page.locator("#content").evaluate("e => getComputedStyle(e).paddingTop") == "0px"
    finally:
        context.close()
