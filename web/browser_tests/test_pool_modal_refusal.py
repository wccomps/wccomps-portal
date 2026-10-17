"""The finding form's IP pool modal shows a refusal's reason (a CSRF failure, a 403 or 404 is a JSON error with no
field errors), instead of throwing and saying the pool couldn't be created for no reason."""

import json

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_the_pool_modal_shows_a_refusals_reason(live_server, pw_browser):
    context = create_session_context(pw_browser, live_server, role_user("red_team"))
    page = context.new_page()
    try:
        page.goto(live_server.url + reverse("scoring:submit_red_score"))
        page.route(
            "**" + reverse("scoring:ip_pool_create"),
            lambda route: route.fulfill(
                status=403, content_type="application/json", body=json.dumps({"error": "Refused for a reason"})
            ),
        )
        page.evaluate("openCreatePoolModal()")
        page.fill("#poolName", "Pool A")
        page.fill("#poolIps", "10.0.0.1")
        page.evaluate("submitCreatePool()")
        error = page.locator("#poolFormError")
        error.wait_for(state="visible")
        assert "Refused for a reason" in error.inner_text()
    finally:
        context.close()
