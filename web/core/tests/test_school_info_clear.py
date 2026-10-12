"""Clearing school info empties the list and the open event's assignments; schools and past events stay."""

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from registration.models import Event, EventTeamAssignment, School, Season

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _setup(open_event):
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    past = Event.objects.create(season=season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    event = Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=open_event
    )
    team = Team.objects.create(team_number=1, team_name="Team 01")
    school = School.for_name("Example University")
    SchoolInfo.objects.create(team=team, school=school, contact_email="c@example.edu")
    EventTeamAssignment.objects.create(event=past, team=team, school=School.for_name("Old College"))
    EventTeamAssignment.objects.create(event=event, team=team, school=school)
    return past, event


def test_clear_with_an_open_event_removes_its_assignments_and_keeps_past_history(client):
    past, _ = _setup(open_event=True)

    client.post(reverse("school_info_clear"))

    assert not SchoolInfo.objects.exists()
    assert School.objects.count() == 2
    assert list(EventTeamAssignment.objects.values_list("event", flat=True)) == [past.pk]


def test_clear_with_no_open_event_keeps_every_assignment(client):
    _setup(open_event=False)

    client.post(reverse("school_info_clear"))

    assert not SchoolInfo.objects.exists()
    assert School.objects.count() == 2
    assert EventTeamAssignment.objects.count() == 2


def test_the_confirm_page_says_what_is_kept_and_removed(client):
    content = client.get(reverse("school_info_clear")).content.decode()

    assert (
        "Schools and their past-event history are kept; the open event's team assignments are removed with the list."
    ) in content
