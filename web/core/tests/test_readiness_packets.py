"""The packets check: packets went out for the open event, and every team on the list has a delivered copy."""

from datetime import date

import pytest
from packets.models import Packet, PacketDistribution
from registration.models import Event, Season
from registration.services import set_team_school

from core.admin_views.readiness import _check_packets_distributed
from team.models import Team

pytestmark = pytest.mark.django_db


def _packet(title, event, **fields):
    fields = {"status": "completed", **fields}
    return Packet.objects.create(
        event=event,
        title=title,
        file_data=b"%PDF",
        filename="rules.pdf",
        mime_type="application/pdf",
        file_size=4,
        uploaded_by="gold",
        **fields,
    )


@pytest.fixture
def season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


def _event(season, name, **fields):
    return Event.objects.create(season=season, name=name, event_type="invitational", date=date(2026, 10, 3), **fields)


def _listed_team(number):
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
    set_team_school(team, f"School {number}", contact_email="c@example.edu")
    return team


def test_with_no_event_open_it_warns():
    assert _check_packets_distributed()[:2] == ("warn", "No event is open, so no packet can be sent")


def test_an_open_event_with_no_packet_sent_warns(season):
    event = _event(season, "Qual", is_active=True)
    _packet("Old Rules", _event(season, "Inv"))
    _packet("Draft", event, status="draft")

    status, message, action = _check_packets_distributed()

    assert (status, message) == ("warn", "No packet has been sent for Qual (2026)")
    assert action["label"] == "View Packets"


def test_failed_and_pending_copies_count_as_missing(season):
    event = _event(season, "Qual", is_active=True)
    teams = [_listed_team(n) for n in (1, 2, 3, 4)]
    Team.objects.create(team_number=5, team_name="Team 05", is_active=True)  # Not on the list: no packet goes to it
    packet = _packet("Rules", event)
    PacketDistribution.objects.create(packet=packet, team=teams[0], email_status="sent")
    PacketDistribution.objects.create(packet=packet, team=teams[1], email_status="failed")
    PacketDistribution.objects.create(packet=packet, team=teams[2], email_status="pending")

    assert _check_packets_distributed()[:2] == ("warn", "Rules: 3 teams without a delivered copy")


def test_a_web_only_packet_is_delivered_once_distributed(season):
    event = _event(season, "Qual", is_active=True)
    team = _listed_team(1)
    packet = _packet("Rules", event, send_via_email=False)
    PacketDistribution.objects.create(packet=packet, team=team, email_status="pending")

    assert _check_packets_distributed()[0] == "pass"
