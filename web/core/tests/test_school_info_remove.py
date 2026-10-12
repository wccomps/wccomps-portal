"""Removing one team's school from its School Info page goes through the school list service."""

import html
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from packets.models import Packet
from registration.models import Event, EventTeamAssignment, School, Season
from registration.services import set_team_school

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


def _team_with_school():
    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="c@example.edu")
    return team


def test_the_edit_page_links_to_remove(client):
    _team_with_school()

    content = client.get(reverse("school_info_edit", args=[4])).content.decode()

    assert reverse("school_info_remove", args=[4]) in content


def test_removing_with_no_open_event_drops_only_the_school_info(client):
    _team_with_school()

    confirm = client.get(reverse("school_info_remove", args=[4]))
    response = client.post(reverse("school_info_remove", args=[4]))

    assert "Example University" in confirm.content.decode()
    assert response.status_code == 302
    assert not SchoolInfo.objects.exists()
    assert School.objects.filter(name="Example University").exists()


def test_removing_drops_the_open_events_assignment(client, event):
    _team_with_school()

    client.post(reverse("school_info_remove", args=[4]))

    assert not SchoolInfo.objects.exists()
    assert not EventTeamAssignment.objects.exists()


def test_removing_is_refused_once_packets_went_out_for_the_open_event(client, event):
    _team_with_school()
    Packet.objects.create(
        title="Packet",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        status="completed",
        event=event,
    )

    response = client.post(reverse("school_info_remove", args=[4]))

    assert (
        f"Packets have already been sent for {event}. Change the team's school on its School Info page instead."
        in html.unescape(response.content.decode())
    )
    assert SchoolInfo.objects.exists()
    assert EventTeamAssignment.objects.exists()


def test_a_team_with_no_school_has_nothing_to_remove(client):
    Team.objects.create(team_number=4, team_name="Team 04")

    response = client.get(reverse("school_info_remove", args=[4]))

    assert response.status_code == 404
