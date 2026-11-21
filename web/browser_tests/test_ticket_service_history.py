"""The staff ticket history resolves a casing mismatch against the same team's stored checks."""

import json

import pytest
from django.core.cache import cache
from django.urls import reverse
from playwright.sync_api import expect
from quotient.client import get_quotient_client

from core.tests.external_json import Answer, serving
from ticketing.models import Ticket, TicketCategory

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_staff_history_loads_only_an_unambiguous_stored_name(
    live_server, pw_browser, ticketing_support_user, blue_team, settings, ambiguous
):
    settings.QUOTIENT_API_URL = "https://scorer.example.test"
    settings.QUOTIENT_USERNAME = "test-admin"
    settings.QUOTIENT_PASSWORD = "test-password"  # noqa: S105
    ticket = Ticket.objects.create(
        team=blue_team,
        ticket_number="T001-008",
        category=TicketCategory.objects.get(pk=6),
        title="Web check history",
        hostname="web",
        service_name="web:http",
    )
    names = ["WEB-HTTP", "WEB-DNS"]
    if ambiguous:
        names.append("Web-Http")
    answers = [
        Answer(200, b"{}"),
        Answer(200, b"null"),
        Answer(200, json.dumps([{"ServiceName": name, "Last10Rounds": []} for name in names]).encode()),
        Answer(200, b'[{"Round":{"ID":91,"StartTime":"2026-11-21T19:00:00Z"},"Result":true}]'),
    ]
    cache.clear()
    get_quotient_client.cache_clear()
    try:
        with (
            serving(answers) as requested,
            create_session_context(pw_browser, live_server, ticketing_support_user) as context,
        ):
            local_origin = live_server.url + "/"
            context.route(
                "**/*", lambda route: route.continue_() if route.request.url.startswith(local_origin) else route.abort()
            )
            page = context.new_page()
            failures = []
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.on("console", lambda message: failures.append(message.text) if message.type == "error" else None)
            page.goto(live_server.url + reverse("ticket_detail", args=[ticket.ticket_number]))
            panel = page.locator("#service-checks-panel")
            if ambiguous:
                expect(panel.get_by_text("No service checks recorded for web-http.")).to_be_visible()
            else:
                expect(panel.get_by_role("table", name="Service check history")).to_be_visible()
                expect(panel.get_by_role("cell", name="91", exact=True)).to_be_visible()
                expect(panel.get_by_text("Passed", exact=True)).to_be_visible()
            assert failures == []
            assert [url.removeprefix(settings.QUOTIENT_API_URL) for url in requested] == [
                "/api/login",
                "/api/services/1/web-http",
                "/api/services/1",
                *([] if ambiguous else ["/api/services/1/WEB-HTTP"]),
            ]
    finally:
        cache.clear()
        get_quotient_client.cache_clear()
