"""A tab left open past its session must go back through login, not alert "Network error" every poll."""

import pytest
from playwright.sync_api import Dialog

from .conftest import _create_role_user, create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_ticket_list_poll_after_the_session_expires_goes_to_login(live_server, pw_browser):
    from django.contrib.sessions.models import Session

    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    dialogs: list[str] = []

    def record(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", record)

    try:
        page.clock.install()
        page.goto(f"{live_server.url}/tickets/")
        page.locator("#ticket-list-content").wait_for()

        Session.objects.all().delete()
        with page.expect_navigation(url="**/auth/login/?next=%2Ftickets%2F"):
            page.clock.run_for(31_000)  # the list polls every 30s

        assert dialogs == []
    finally:
        context.close()


def test_a_poll_that_cannot_reach_the_server_does_not_alert(live_server, pw_browser):
    """A tab waking from sleep polls before the network is back; the next poll retries on its own."""
    user = _create_role_user("admin", None)
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    dialogs: list[str] = []

    def record(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", record)

    try:
        page.clock.install()
        page.goto(f"{live_server.url}/tickets/")
        page.locator("#ticket-list-content").wait_for()

        page.route("**/tickets/?*", lambda route: route.abort("internetdisconnected"))
        with page.expect_event("requestfailed"):
            page.clock.run_for(31_000)
        page.evaluate("() => new Promise(requestAnimationFrame)")  # let htmx's error handlers run

        assert dialogs == []
    finally:
        context.close()
