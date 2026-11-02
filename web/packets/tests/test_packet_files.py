"""A packet's size comes from its bytes, and a download sends exactly them."""

import pytest
from django.test import Client
from django.urls import reverse

from packets.models import Packet

pytestmark = pytest.mark.django_db


def _packet(event):
    packet = Packet.with_file(b"%PDF", "rules.pdf", event=event, title="Rules", status="completed", uploaded_by="gold")
    packet.save()
    return packet


def test_a_packet_download_sends_its_bytes_whatever_its_stored_size(gold_team_user, current_event):
    packet = _packet(current_event)
    Packet.objects.filter(pk=packet.pk).update(file_size=999)
    client = Client()
    client.force_login(gold_team_user)
    response = client.get(reverse("download_packet", args=[packet.id]))
    assert response.content == b"%PDF"
    assert response.get("Content-Length") in (None, "4")


def test_a_packets_size_comes_from_its_bytes() -> None:
    assert Packet.with_file(b"12345", "a.txt", title="T", uploaded_by="gold").file_size == 5
