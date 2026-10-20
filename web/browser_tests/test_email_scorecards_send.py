"""Emailing the scorecards keeps the result on the page: no reload, no clearing."""

import json

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context
from .table_data import seed_table_data

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_sending_keeps_the_result_up(live_server, pw_browser):
    admin = role_user("admin")
    seed_table_data(admin)
    context = create_session_context(pw_browser, live_server, admin)
    page = context.new_page()
    lines = [
        {"step": "Team 01", "current": 1, "total": 1, "ok": True},
        {"done": True, "success": True, "message": "Sent 1 scorecard"},
    ]
    page.route(
        f"{live_server.url}{reverse('scoring:stream_email_scorecards')}",
        lambda route: route.fulfill(
            status=200, content_type="application/x-ndjson", body="".join(json.dumps(x) + "\n" for x in lines)
        ),
    )
    try:
        page.goto(live_server.url + reverse("scoring:email_scorecards"))
        page.get_by_role("button", name="Send").click()
        page.get_by_text("Sent 1 scorecard").wait_for()
        page.wait_for_timeout(6000)
        assert page.get_by_text("Sent 1 scorecard").is_visible(), "the result must stay (no reload, no clearing)"
        assert page.get_by_role("button", name="Back to Leaderboard").is_visible()
    finally:
        context.close()
