"""A team sees only the open event's packets; sending stamps the packet with the open event."""

import json
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from registration.models import Event

from packets.models import Packet, PacketDistribution
from packets.services import PacketDistributionService

pytestmark = pytest.mark.django_db


def _packet(event, title="Rules", status="completed"):
    return Packet.objects.create(
        event=event,
        title=title,
        file_data=b"%PDF",
        filename="rules.pdf",
        mime_type="application/pdf",
        file_size=4,
        status=status,
        uploaded_by="gold",
    )


@pytest.fixture
def team_1(db):
    from team.models import Team

    return Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01", "is_active": True})[0]


def test_team_sees_only_the_open_events_packets(blue_team_user, team_1, current_event):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    PacketDistribution.objects.create(packet=_packet(earlier, "Old"), team=team_1)
    PacketDistribution.objects.create(packet=_packet(current_event, "Current"), team=team_1)
    client = Client()
    client.force_login(blue_team_user)

    content = client.get(reverse("team_packet")).content.decode()

    assert "Current" in content
    assert "Old" not in content


def test_an_earlier_events_packet_cannot_be_downloaded(blue_team_user, team_1, current_event):
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    packet = _packet(earlier)
    PacketDistribution.objects.create(packet=packet, team=team_1)
    client = Client()
    client.force_login(blue_team_user)

    assert client.get(reverse("download_packet", args=[packet.id])).status_code == 404


def test_no_open_event_means_no_packets(blue_team_user, team_1):
    packet = _packet(None)
    PacketDistribution.objects.create(packet=packet, team=team_1)
    client = Client()
    client.force_login(blue_team_user)

    assert client.get(reverse("download_packet", args=[packet.id])).status_code == 404


def test_sending_stamps_the_open_event(current_event):
    packet = _packet(None, status="draft")

    lines = list(PacketDistributionService().stream_distribute_packet(packet))

    packet.refresh_from_db()
    assert packet.event == current_event
    assert json.loads(lines[-1])["done"]


def test_sending_with_no_open_event_is_refused():
    packet = _packet(None, status="draft")

    lines = list(PacketDistributionService().stream_distribute_packet(packet))

    result = json.loads(lines[-1])
    assert result == {"done": True, "success": False, "message": "Import the school list for this event first"}
    packet.refresh_from_db()
    assert packet.status == "draft"
