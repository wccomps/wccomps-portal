"""The nav bar's tools: the dark mode button says what the page shows, and the new-ticket toast stays up while the
pointer or keyboard focus is on it."""

import pytest
from playwright.sync_api import expect

from core.tests.roles import role_user

from .conftest import create_session_context, make_test_data

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_the_dark_mode_button_follows_the_os_until_clicked(live_server, pw_browser):
    """With no saved choice the page follows the OS, while it stays open too: the button reads pressed and shows the
    moon whenever the page is dark."""
    context = create_session_context(pw_browser, live_server, role_user("gold_team"))
    page = context.new_page()
    try:
        page.emulate_media(color_scheme="light")
        page.goto(f"{live_server.url}/ops/admin/teams/")
        button = page.get_by_role("button", name="Dark mode")
        moon = button.locator("span").nth(1)
        assert button.get_attribute("aria-pressed") == "false"
        assert not moon.is_visible()

        page.emulate_media(color_scheme="dark")
        expect(button).to_have_attribute("aria-pressed", "true")
        assert moon.is_visible()

        button.click()
        assert button.get_attribute("aria-pressed") == "false"
        assert page.evaluate("document.documentElement.dataset.theme") == "light"
    finally:
        context.close()


def test_the_toast_stays_while_focused_after_the_pointer_leaves(live_server, pw_browser):
    make_test_data()  # an open ticket, which the first poll announces
    context = create_session_context(pw_browser, live_server, role_user("ticketing_support"))
    page = context.new_page()
    try:
        page.clock.install()
        with page.expect_response(lambda r: "/tickets/notifications/" in r.url):
            page.goto(f"{live_server.url}/tickets/")
        page.clock.run_for(500)  # the slide-in transition runs on the page's clock
        toast = page.locator(".ticket-toast")
        toast.wait_for()

        toast.focus()
        toast.hover()
        page.mouse.move(0, 0)
        page.clock.run_for("00:10")
        assert toast.is_visible()

        page.evaluate("document.activeElement.blur()")
        page.clock.run_for("00:03")
        toast.wait_for(state="hidden")
    finally:
        context.close()
