"""Clearing school info empties the current list but keeps the season's schools and assignments."""

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from registration.models import Event, EventTeamAssignment, School, Season
from registration.services import assign_school_to_event

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


def test_clear_keeps_schools_and_assignments(gold_team_user):
    team = Team.objects.create(team_number=1, team_name="Team 01")
    SchoolInfo.objects.create(team=team, school=School.for_name("Example University"), contact_email="c@example.edu")
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3))
    assign_school_to_event(event, team, "Example University")
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("school_info_clear"))

    assert not SchoolInfo.objects.exists()
    assert School.objects.count() == 1
    assert EventTeamAssignment.objects.count() == 1
