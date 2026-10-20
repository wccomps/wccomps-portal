"""The error page (core.errors) in a browser: a refused role and a missing page get it, styled as every page is, with
no script errors, and the 403 links to the person's own landing page."""

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context, visit_and_capture_errors

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def _script_errors(errors: list[str]) -> list[str]:
    """The page's errors, less the browser's own note that the page itself answered an error status."""
    return [e for e in errors if "Failed to load resource" not in e]


def test_a_refused_role_gets_the_403_page_with_a_link_home(live_server, pw_browser):
    context = create_session_context(pw_browser, live_server, role_user("blue_team"))
    try:
        page, status, errors = visit_and_capture_errors(context, live_server.url + reverse("admin_teams"))
        assert status == 403
        assert page.locator(".breadcrumbs", has_text="No access").count() == 1
        home = page.get_by_role("button", name="Go to your home page")
        assert home.get_attribute("href") == reverse("ticket_list")
        home.click()
        page.wait_for_url(f"**{reverse('ticket_list')}")
        assert not _script_errors(errors)
    finally:
        context.close()


def test_a_missing_page_gets_the_404_page(live_server, pw_browser):
    context = create_session_context(pw_browser, live_server, role_user("red_team"))
    try:
        page, status, errors = visit_and_capture_errors(context, live_server.url + "/no/such/page/")
        assert status == 404
        assert page.locator(".breadcrumbs", has_text="Not found").count() == 1
        assert page.get_by_role("button", name="Go to your home page").get_attribute("href") == reverse(
            "scoring:submit_red_score"
        )
        assert not _script_errors(errors)
    finally:
        context.close()
