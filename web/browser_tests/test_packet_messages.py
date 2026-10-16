"""Packet page messages show their real outcome: a failure is red and stays up, not a green box."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from core.tests.roles import role_user

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_failed_test_email_shows_a_red_message_that_stays(live_server, pw_browser):
    from packets.models import Packet

    from team.models import Team

    Team.objects.create(team_number=1, team_name="Team 1", is_active=True)
    packet = Packet.objects.create(
        title="Packet", file_data=b"x", filename="p.pdf", mime_type="application/pdf", file_size=1, uploaded_by="gold"
    )
    user = role_user("admin")
    context = create_session_context(pw_browser, live_server, user)
    page = context.new_page()

    try:
        with patch(
            "packets.services.PacketDistributionService.send_test_packet_email",
            side_effect=Exception("(535, b'Authentication Failed')"),
        ):
            page.goto(live_server.url + reverse("packet_detail", args=[packet.id]))
            page.locator("#test-email").fill("gold@example.com")
            with page.expect_response(lambda r: f"/packets/{packet.id}/action/" in r.url):
                page.get_by_role("button", name="Send Test").click()

            alert = page.locator("[role=alert]", has_text="Authentication Failed")
            alert.wait_for()
            assert "alert--error" in (alert.get_attribute("class") or "")
            page.wait_for_timeout(6000)
            assert alert.is_visible(), "a failure must stay on screen past the 5s auto-clear"
    finally:
        context.close()
