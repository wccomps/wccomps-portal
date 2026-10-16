"""The browser reports its timezone to the server so times are shown and read in it."""

import pytest

from core.tests.roles import role_user

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_page_load_sets_the_timezone_cookie(live_server, pw_browser):
    from django.test import Client

    user = role_user("ticketing_support")
    client = Client()
    client.force_login(user)
    context = pw_browser.new_context(timezone_id="America/New_York")
    context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value, "url": live_server.url}])
    try:
        page = context.new_page()
        page.goto(f"{live_server.url}/tickets/")
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        assert cookies.get("tz") == "America%2FNew_York"
    finally:
        context.close()
