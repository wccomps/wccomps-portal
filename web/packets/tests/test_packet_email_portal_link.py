"""Packet emails tell the team where to download the packet in the portal."""

import pytest
from django.core import mail
from django.test import override_settings
from registration.models import School

from packets.models import Packet, PacketDistribution
from packets.services import PacketDistributionService
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db

PORTAL_LINE = "You can also download it in the portal:"
PORTAL_URL = "https://portal.example.org/packet/"


@pytest.fixture
def team():
    team = Team.objects.create(team_number=7, team_name="Team 07", is_active=True)
    SchoolInfo.objects.create(
        team=team, school=School.for_name("School 7"), contact_email="team7@example.edu", password="pw"
    )
    return team


@pytest.fixture
def packet():
    return Packet.objects.create(
        title="Rules",
        file_data=b"%PDF",
        filename="rules.pdf",
        mime_type="application/pdf",
        file_size=4,
        uploaded_by="gold",
        send_via_email=True,
        web_access_enabled=True,
    )


def _bodies(message):
    return [message.body, message.alternatives[0][0]]


@override_settings(BASE_URL="https://portal.example.org/")
@pytest.mark.parametrize("send", ["distribution", "test"])
def test_the_email_links_the_team_packet_page(team, packet, send):
    service = PacketDistributionService()
    if send == "distribution":
        service.send_packet_email(PacketDistribution.objects.create(packet=packet, team=team))
    else:
        service.send_test_packet_email(packet, team, "gold@example.org")

    (message,) = mail.outbox
    for body in _bodies(message):
        assert PORTAL_LINE in body
        assert PORTAL_URL in body


@override_settings(BASE_URL="https://portal.example.org")
def test_no_link_when_the_packet_is_not_on_the_web(team, packet):
    packet.web_access_enabled = False
    packet.save()

    PacketDistributionService().send_test_packet_email(packet, team, "gold@example.org")

    (message,) = mail.outbox
    for body in _bodies(message):
        assert "in the portal" not in body


def test_packet_email_and_portal_page_show_configured_competition_schedule(team, packet, blue_team_user):
    from datetime import UTC, datetime

    from django.test import Client
    from django.urls import reverse

    from core.models import CompetitionConfig

    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "competition_start_time": datetime(2026, 10, 3, 16, 0, tzinfo=UTC),
            "competition_end_time": datetime(2026, 10, 4, 0, 0, tzinfo=UTC),
        },
    )
    PacketDistribution.objects.create(packet=packet, team=team)

    PacketDistributionService().send_test_packet_email(packet, team, "gold@example.org")

    (message,) = mail.outbox
    for body in _bodies(message):
        assert "Competition Schedule:" in body
        assert "Oct 3, 2026, 9:00 AM PDT" in body
        assert "Oct 3, 2026, 5:00 PM PDT" in body

    client = Client()
    client.force_login(blue_team_user)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("packets.views.get_user_team", lambda _u: team)
        response = client.get(reverse("team_packet"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Competition Schedule" in html
    assert "Oct 3, 2026, 9:00 AM PDT" in html
    assert "Oct 3, 2026, 5:00 PM PDT" in html


def test_packet_email_and_portal_page_omit_schedule_when_not_configured(team, packet, blue_team_user):
    from django.test import Client
    from django.urls import reverse

    from core.models import CompetitionConfig

    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "competition_start_time": None,
            "competition_end_time": None,
        },
    )
    PacketDistribution.objects.create(packet=packet, team=team)

    PacketDistributionService().send_test_packet_email(packet, team, "gold@example.org")

    (message,) = mail.outbox
    for body in _bodies(message):
        assert "Competition Schedule:" not in body

    client = Client()
    client.force_login(blue_team_user)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("packets.views.get_user_team", lambda _u: team)
        response = client.get(reverse("team_packet"))

    assert response.status_code == 200
    assert "Competition Schedule" not in response.content.decode()
