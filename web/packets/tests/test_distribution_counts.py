import re

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from packets.models import Packet, PacketDistribution
from team.models import Team

pytestmark = pytest.mark.django_db


def _packet(title: str, status: str, sends: list[tuple[str, bool]]) -> Packet:
    """A packet with one distribution per (email_status, downloaded) pair, each to its own team."""
    packet = Packet.objects.create(
        title=title,
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        status=status,
    )
    for number, (email_status, downloaded) in enumerate(sends, start=1):
        team, _ = Team.objects.get_or_create(team_number=number, defaults={"team_name": f"Team {number:02d}"})
        PacketDistribution.objects.create(
            packet=packet,
            team=team,
            email_status=email_status,
            downloaded_at=timezone.now() if downloaded else None,
        )
    return packet


@pytest.fixture
def packets() -> tuple[Packet, Packet]:
    active = _packet(
        "Active",
        "distributing",
        [("sent", True)] + [("sent", False)] * 3 + [("failed", False)] * 3 + [("pending", False)] * 2,
    )
    sent = _packet("Sent", "completed", [("sent", True)] * 2 + [("sent", False)] + [("failed", False)] * 2)
    return active, sent


@pytest.fixture
def client(admin_user) -> Client:
    client = Client()
    client.force_login(admin_user)
    return client


def _text(response) -> str:
    assert response.status_code == 200
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.content.decode()))


def test_the_packet_list_shows_the_active_and_past_packets_counts(client, packets):
    text = _text(client.get(reverse("packets_list")))

    for shown in ("9 teams", "4 sent", "2 pending", "3 failed", "1 downloaded"):
        assert f" {shown} " in text
    assert " 3 sent, 2 downloaded , 2 failed " in text


def test_the_packet_page_shows_its_counts(client, packets):
    text = _text(client.get(reverse("packet_detail", args=[packets[0].pk])))

    for shown in ("9 teams", "4 sent", "2 pending", "3 failed", "1 downloaded"):
        assert f" {shown} " in text


def test_the_packet_admin_shows_the_counts(client, packets):
    change = _text(client.get(reverse("admin:packets_packet_change", args=[packets[0].pk])))
    changelist = _text(client.get(reverse("admin:packets_packet_changelist")))

    assert " Total: 9 Sent: 4 Failed: 3 Downloaded: 1 " in change
    assert " Total: 9 Sent: 4 Failed: 3 Downloaded: 1 " in changelist
    assert " Total: 5 Sent: 3 Failed: 2 Downloaded: 2 " in changelist
