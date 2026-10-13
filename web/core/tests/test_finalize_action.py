"""The ops page's Finalize streams progress and freezes the open event (or the current list's new event)."""

import json
import threading
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse
from registration.models import Event, EventResult, Season
from registration.services import (
    FinalizeError,
    finalize_event,
    open_event_for_current_schools,
    set_team_school,
    snapshot_matches_live,
)
from scoring.models import OrangeTeamScore, ServiceScore

from core.models import CompetitionConfig
from team.models import Team

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _finalize(client, **data):
    response = client.post(reverse("admin_competition_action"), {"action": "finalize_event", **data})
    lines = [json.loads(line) for line in b"".join(response.streaming_content).decode().splitlines()]
    for thread in threading.enumerate():
        if thread.name == "run-detached":
            thread.join()
    return lines[-1]


def _team_with_school(number=1):
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
    set_team_school(team, f"School {number}", contact_email="c@example.edu")
    ServiceScore.objects.create(team=team, service_points=Decimal(100))
    return team


def _season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


def test_refused_while_the_competition_runs(client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    assert _finalize(client) == {"done": True, "success": False, "message": "Stop the competition before finalizing."}


def test_no_open_event_needs_a_name(client):
    _season()

    result = _finalize(client)

    assert not result["success"] and "name" in result["message"]


def test_no_open_event_builds_it_from_the_current_list(client):
    _season()
    _team_with_school()

    result = _finalize(client, event_name="Invitational 1", event_date="2026-10-03", event_type="invitational")

    assert result["success"]
    assert EventResult.objects.get().event.name == "Invitational 1"


def test_unknown_event_type_is_refused(client):
    _season()
    _team_with_school()

    result = _finalize(client, event_name="Invitational 1", event_date="2026-10-03", event_type="bogus")

    assert not result["success"] and "type" in result["message"]
    assert not Event.objects.exists()


def test_name_already_used_in_the_season_is_refused(client):
    season = _season()
    Event.objects.create(season=season, name="Invitational 1", event_type="qualifier", date=date(2026, 9, 1))
    _team_with_school()

    result = _finalize(client, event_name="Invitational 1", event_date="2026-10-03", event_type="invitational")

    assert not result["success"] and "already has an event named" in result["message"]
    assert Event.objects.count() == 1


def test_service_turns_a_duplicate_name_into_finalize_error():
    season = _season()
    Event.objects.create(season=season, name="Invitational 1", event_type="qualifier", date=date(2026, 9, 1))

    with pytest.raises(FinalizeError, match="already has an event named"):
        open_event_for_current_schools("Invitational 1", date(2026, 10, 3), "invitational")
    assert Event.objects.open() is None


def test_service_turns_a_numbering_clash_into_finalize_error():
    season = _season()
    Event.objects.create(season=season, name="Inv A", event_type="invitational", event_number=1, date=date(2026, 9, 1))
    Event.objects.update(event_number=None)
    with pytest.raises(FinalizeError), patch.object(Event.objects, "create", side_effect=IntegrityError):
        open_event_for_current_schools("Inv B", date(2026, 10, 3), "invitational")
    assert Event.objects.open() is None


def test_finalizes_the_open_event(client):
    event = Event.objects.create(
        season=_season(), name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    _team_with_school()

    assert _finalize(client)["success"]
    assert EventResult.objects.filter(event=event).count() == 1


def test_same_description_orange_rows_keep_the_snapshot_matching():
    event = Event.objects.create(
        season=_season(), name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    team = _team_with_school()
    for points in (5, 7, 3, 9):
        OrangeTeamScore.objects.create(team=team, description="Same", points_awarded=Decimal(points), is_approved=True)

    finalize_event(event, "tester")

    assert snapshot_matches_live(event)
