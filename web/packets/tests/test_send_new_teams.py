"""Sending a packet to teams added to the open event after it went out: only they get it, by email and on the web."""

import json
from datetime import date

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from registration.models import Event, School

from packets.models import Packet, PacketDistribution
from packets.services import PacketDistributionService
from team.models import SchoolInfo, Team

# The emails go out from worker threads, which see only committed rows
pytestmark = pytest.mark.django_db(transaction=True)


def _team_with_school(number):
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
    SchoolInfo.objects.create(
        team=team, school=School.for_name(f"School {number}"), contact_email=f"team{number}@example.edu", password="pw"
    )
    return team


def _packet(event, status="completed"):
    return Packet.objects.create(
        event=event,
        title="Rules",
        file_data=b"%PDF",
        filename="rules.pdf",
        mime_type="application/pdf",
        file_size=4,
        status=status,
        uploaded_by="gold",
        send_via_email=True,
        web_access_enabled=True,
    )


def _done(lines):
    return json.loads(lines[-1])


def test_only_teams_without_the_packet_get_it(current_event):
    had_it, late = _team_with_school(1), _team_with_school(2)
    packet = _packet(current_event)
    PacketDistribution.objects.create(packet=packet, team=had_it, email_status="sent")

    result = _done(list(PacketDistributionService().stream_send_to_new_teams(packet)))

    assert result["success"] is True
    assert [message.to for message in mail.outbox] == [["team2@example.edu"]]
    assert PacketDistribution.objects.get(packet=packet, team=late).email_status == "sent"
    assert PacketDistribution.objects.get(packet=packet, team=had_it).email_status == "sent"
    assert PacketDistribution.objects.filter(packet=packet).count() == 2
    packet.refresh_from_db()
    assert packet.status == "completed"


def test_a_failed_team_is_left_to_resend_failed(current_event):
    failed = _team_with_school(1)
    packet = _packet(current_event)
    PacketDistribution.objects.create(packet=packet, team=failed, email_status="failed")

    result = _done(list(PacketDistributionService().stream_send_to_new_teams(packet)))

    assert result == {
        "done": True,
        "success": True,
        "message": (
            "No team on the list is without a copy of this packet; teams whose send failed or is pending are not "
            "included (use Resend Failed / Retry Pending)."
        ),
    }
    assert not mail.outbox
    assert PacketDistribution.objects.get(packet=packet).email_status == "failed"


def test_a_packet_from_another_event_is_refused(current_event):
    _team_with_school(1)
    earlier = Event.objects.create(
        season=current_event.season, name="Earlier", event_type="invitational", date=date(2026, 9, 1)
    )
    packet = _packet(earlier)

    result = _done(list(PacketDistributionService().stream_send_to_new_teams(packet)))

    assert result["success"] is False
    assert "earlier event" in result["message"]
    assert not PacketDistribution.objects.exists()
    assert not mail.outbox


def test_a_draft_packet_is_distributed_instead(current_event):
    _team_with_school(1)
    packet = _packet(current_event, status="draft")

    result = _done(list(PacketDistributionService().stream_send_to_new_teams(packet)))

    assert result == {
        "done": True,
        "success": False,
        "message": "Only a sent packet can be sent to new teams.",
    }
    assert not PacketDistribution.objects.exists()


def test_the_new_team_can_download_it(current_event, blue_team_user):
    _team_with_school(1)
    packet = _packet(current_event)
    list(PacketDistributionService().stream_send_to_new_teams(packet))
    client = Client()
    client.force_login(blue_team_user)

    response = client.get(reverse("download_packet", args=[packet.id]))

    assert response.status_code == 200
    assert response.content == b"%PDF"


def test_the_packet_action_sends_to_new_teams(current_event, gold_team_user):
    _team_with_school(1)
    packet = _packet(current_event)
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("packet_action", args=[packet.id]), {"action": "send_new_teams"})

    lines = b"".join(response.streaming_content).decode().splitlines()
    assert _done(lines)["success"] is True
    assert [message.to for message in mail.outbox] == [["team1@example.edu"]]


@pytest.mark.parametrize(("status", "shown"), [("draft", False), ("distributing", True), ("completed", True)])
def test_the_button_shows_for_sent_packets(current_event, gold_team_user, status, shown):
    packet = _packet(current_event, status=status)
    client = Client()
    client.force_login(gold_team_user)

    content = client.get(reverse("packet_detail", args=[packet.id])).content.decode()

    assert ("Send to Teams Without It" in content) is shown


def test_the_done_line_names_new_teams_without_per_team_extras(current_event):
    _team_with_school(41), _team_with_school(42), _team_with_school(43)
    packet = _packet(current_event)
    packet.team_extras = {"43": {"api_key": "k"}}
    packet.save()

    result = _done(list(PacketDistributionService().stream_send_to_new_teams(packet)))

    assert result == {
        "done": True,
        "success": True,
        "message": "Sent to 3 new team(s). No per-team extras for Team 41, Team 42.",
    }


def test_without_per_team_extras_the_done_line_does_not_mention_them(current_event):
    _team_with_school(41)

    result = _done(list(PacketDistributionService().stream_send_to_new_teams(_packet(current_event))))

    assert result["message"] == "Sent to 1 new team(s)"


@pytest.mark.parametrize(("status", "shown"), [("distributing", True), ("completed", True)])
def test_retry_pending_shows_for_sent_packets_with_pending_teams(current_event, gold_team_user, status, shown):
    packet = _packet(current_event, status=status)
    PacketDistribution.objects.create(packet=packet, team=_team_with_school(1), email_status="pending")
    client = Client()
    client.force_login(gold_team_user)

    content = client.get(reverse("packet_detail", args=[packet.id])).content.decode()

    assert ("Retry Pending" in content) is shown
    assert "1 pending" in content


def test_retry_pending_sends_a_completed_packets_pending_teams(current_event):
    packet = _packet(current_event)
    PacketDistribution.objects.create(packet=packet, team=_team_with_school(1), email_status="pending")

    result = _done(list(PacketDistributionService().stream_retry_pending(packet)))

    assert result["success"] is True
    assert [message.to for message in mail.outbox] == [["team1@example.edu"]]
