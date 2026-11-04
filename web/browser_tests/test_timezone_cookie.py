"""The browser reports its timezone to the server so times are shown and read in it."""

from datetime import UTC, datetime

import pytest
from orange_team.models import OrangeCheck

from core.tests.roles import role_user

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_page_load_sets_the_timezone_cookie_and_renders_local_timestamps(live_server, pw_browser):
    from django.test import Client

    user = role_user("orange_team_lead")
    check = OrangeCheck.objects.create(
        title="Firewall Check",
        description="Verify rules",
        scheduled_at=datetime(2026, 10, 3, 16, 30, tzinfo=UTC),
        created_by=user,
    )
    client = Client()
    client.force_login(user)
    context = pw_browser.new_context(timezone_id="America/New_York")
    context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value, "url": live_server.url}])
    try:
        page = context.new_page()
        page.goto(f"{live_server.url}/orange-team/")
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        assert cookies.get("tz") == "America%2FNew_York"

        page.goto(f"{live_server.url}/orange-team/checks/{check.pk}/")
        assert "Oct 03, 2026 12:30" in page.locator("#content").inner_text()
    finally:
        context.close()
