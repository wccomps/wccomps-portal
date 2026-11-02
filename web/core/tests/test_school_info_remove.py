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


def _shown():
    """The school the confirm page showed, posted back with the form."""
    return {"school": SchoolInfo.objects.get().school_id}


def test_the_edit_page_links_to_remove(client):
    _team_with_school()

    content = client.get(reverse("school_info_edit", args=[4])).content.decode()

    assert reverse("school_info_remove", args=[4]) in content


def test_removing_with_no_open_event_drops_only_the_school_info(client):
    _team_with_school()

    confirm = client.get(reverse("school_info_remove", args=[4]))
    response = client.post(reverse("school_info_remove", args=[4]), _shown())

    assert "Example University" in confirm.content.decode()
    assert response.status_code == 302
    assert not SchoolInfo.objects.exists()
    assert School.objects.filter(name="Example University").exists()


def test_removing_drops_the_open_events_assignment(client, event):
    _team_with_school()

    client.post(reverse("school_info_remove", args=[4]), _shown())

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

    response = client.post(reverse("school_info_remove", args=[4]), _shown())

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


def test_removing_is_refused_when_the_school_changed_since_the_page_was_opened(client, event):
    team = _team_with_school()
    page = client.get(reverse("school_info_remove", args=[4])).content.decode()
    shown = _shown()
    set_team_school(team, "Other College", contact_email="o@example.edu")

    response = client.post(reverse("school_info_remove", args=[4]), shown)

    assert f'name="school" value="{shown["school"]}"' in page
    assert "Team 4&#x27;s school changed since you opened this page; reload." in response.content.decode()
    assert SchoolInfo.objects.get().school_name == "Other College"
    assert EventTeamAssignment.objects.get().school.name == "Other College"


def test_removing_is_refused_while_members_are_linked(client):
    """Removing, then adding a school, would skip the unlink and the password reset a different school gets."""
    from django.contrib.auth.models import User
    from registration.services import LINKED_TEAM_REMOVE

    from team.models import DiscordLink

    team = _team_with_school()
    user = User.objects.create(username="member04")
    DiscordLink.objects.create(discord_id=44, discord_username="m", user=user, team=team, is_active=True)

    response = client.post(reverse("school_info_remove", args=[4]), _shown())

    assert html.escape(LINKED_TEAM_REMOVE) in response.content.decode()
    assert SchoolInfo.objects.filter(team=team).exists()
