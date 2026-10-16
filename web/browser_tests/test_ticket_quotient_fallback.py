"""With Quotient down, a team can still file tickets that need a hostname, IP, or service."""

from unittest.mock import MagicMock, patch

import pytest

from core.tests.roles import role_user
from team.models import Team

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def _unavailable_quotient() -> MagicMock:
    client = MagicMock()
    client.get_infrastructure.return_value = None
    client.get_box_names.return_value = []
    client.get_service_choices.return_value = []
    return client


@pytest.mark.parametrize(
    ("category", "fields"),
    [
        (2, {"#hostname": "web01", "#ip_address": "10.0.1.5"}),  # Box Reset: hostname + IP required
        (3, {"#service_name": "web01 HTTP"}),  # Scoring Service Check: service required
    ],
)
def test_ticket_filed_with_typed_fields(category, fields, live_server, pw_browser):
    from ticketing.models import Ticket

    Team.objects.create(team_number=1, team_name="Team 01")  # the blue_team role's team, by its Authentik group
    user = role_user("blue_team")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()
    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    try:
        with patch("quotient.client.get_quotient_client", return_value=_unavailable_quotient()):
            page.goto(f"{live_server.url}/tickets/create/")
            assert page.get_by_text("Scoring engine is unavailable").is_visible()

            page.select_option("#category", str(category))
            page.fill("#title", f"Fallback ticket {category}")
            for selector, value in fields.items():
                page.locator(selector).fill(value)
            with page.expect_navigation():
                page.click("button[type=submit]")

        ticket = Ticket.objects.get(title=f"Fallback ticket {category}")
        assert ticket.hostname == fields.get("#hostname", "")
        assert ticket.ip_address in (fields.get("#ip_address", ""), None)
        assert ticket.service_name == fields.get("#service_name", "")
        assert not console_errors, console_errors
    finally:
        context.close()
