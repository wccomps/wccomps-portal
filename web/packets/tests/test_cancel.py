"""Only a draft packet can be cancelled: a sent one reached the teams, and the school list guards count it as sent."""

import json

import pytest
from django.test import Client
from django.urls import reverse
from registration.services import PacketsSentError, clear_school_list

from packets.models import Packet

pytestmark = pytest.mark.django_db


def _packet(event, status):
    return Packet.objects.create(
        event=event,
        title="Rules",
        file_data=b"%PDF",
        filename="rules.pdf",
        mime_type="application/pdf",
        file_size=4,
        status=status,
        uploaded_by="gold",
    )


def _cancel(user, packet):
    client = Client()
    client.force_login(user)
    response = client.post(reverse("packet_action", args=[packet.pk]), {"action": "cancel"})
    return json.loads(b"".join(response.streaming_content).decode().strip().splitlines()[-1])


@pytest.mark.parametrize("status", ["distributing", "completed"])
def test_cancelling_a_sent_packet_is_refused(gold_team_user, current_event, status):
    packet = _packet(current_event, status)

    done = _cancel(gold_team_user, packet)

    packet.refresh_from_db()
    assert packet.status == status
    assert done["success"] is False
    assert "Only a draft packet can be cancelled" in done["message"]
    with pytest.raises(PacketsSentError):
        clear_school_list()


def test_a_draft_can_be_cancelled(gold_team_user, current_event):
    packet = _packet(current_event, "draft")

    done = _cancel(gold_team_user, packet)

    packet.refresh_from_db()
    assert packet.status == "cancelled"
    assert done["success"] is True


@pytest.mark.parametrize("page", ["detail", "list"])
@pytest.mark.parametrize(("status", "shown"), [("draft", True), ("distributing", False), ("completed", False)])
def test_the_cancel_button_shows_only_for_drafts(gold_team_user, current_event, page, status, shown):
    packet = _packet(current_event, status)
    client = Client()
    client.force_login(gold_team_user)
    url = reverse("packet_detail", args=[packet.pk]) if page == "detail" else reverse("packets_list")

    content = client.get(url).content.decode()

    assert ('data-action="cancel"' in content) is shown
