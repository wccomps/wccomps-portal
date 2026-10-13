"""The ops page's Finalize streams progress and freezes the open event (or the current list's new event)."""

import json
import threading
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from registration.models import Event, EventResult, Season
from registration.services import (
    FinalizeError,
    NewEvent,
    clear_event,
    finalize_open_event,
    set_team_school,
)
from scoring.calculator import compute_standings
from scoring.models import InjectScore, OrangeTeamScore, ServiceDetail, ServiceScore
from scoring.scorecard import build_scorecard_context

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


def _open_event():
    return Event.objects.create(
        season=_season(), name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


def test_refused_while_the_competition_runs(client):
    event = _open_event()
    _team_with_school()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    assert _finalize(client, open_event=event.pk) == {
        "done": True,
        "success": False,
        "message": "Stop the competition before finalizing.",
    }


def test_no_open_event_needs_a_name(client):
    _season()

    result = _finalize(client)

    assert result == {
        "done": True,
        "success": False,
        "message": "No event is open: give the event a name, a date and a valid type to finalize the current list.",
    }


def test_an_event_name_over_255_characters_is_refused(client):
    _season()
    _team_with_school()

    result = _finalize(client, event_name="x" * 256, event_date="2026-10-03", event_type="invitational")

    assert result == {"done": True, "success": False, "message": "The event name must be at most 255 characters."}
    assert not Event.objects.exists()


def test_no_open_event_builds_it_from_the_current_list(client):
    _season()
    _team_with_school()

    result = _finalize(client, event_name="Invitational 1", event_date="2026-10-03", event_type="invitational")

    assert result == {"done": True, "success": True, "message": "Finalized Invitational 1: 1 school result(s)."}
    assert EventResult.objects.get().event.name == "Invitational 1"


def test_unknown_event_type_is_refused(client):
    _season()
    _team_with_school()

    result = _finalize(client, event_name="Invitational 1", event_date="2026-10-03", event_type="bogus")

    assert result == {"done": True, "success": False, "message": "Choose a valid event type."}
    assert not Event.objects.exists()


def test_name_already_used_in_the_season_is_refused(client):
    season = _season()
    Event.objects.create(season=season, name="Invitational 1", event_type="qualifier", date=date(2026, 9, 1))
    _team_with_school()

    result = _finalize(client, event_name="Invitational 1", event_date="2026-10-03", event_type="invitational")

    assert not result["success"] and "already has an event named" in result["message"]
    assert Event.objects.count() == 1


def test_a_new_event_is_refused_when_an_event_opened_since_the_page_loaded(client):
    """The page showed the new event's fields; the open event decides under the lock, not at the request."""
    _team_with_school()
    _open_event()

    result = _finalize(client, event_name="Invitational 1", event_date="2026-10-03", event_type="invitational")

    assert result == {
        "done": True,
        "success": False,
        "message": "Inv (2026) was opened since the page loaded. Reload the page to finalize it.",
    }
    assert not EventResult.objects.exists()


def test_the_open_event_shown_by_the_page_must_still_be_open(client):
    event = _open_event()
    _team_with_school()
    Event.objects.filter(pk=event.pk).update(is_active=False)

    result = _finalize(client, open_event=event.pk)

    assert result == {"done": True, "success": False, "message": "Inv (2026) is no longer open. Reload the page."}
    assert not EventResult.objects.exists()


def test_service_turns_a_duplicate_name_into_finalize_error():
    season = _season()
    Event.objects.create(season=season, name="Invitational 1", event_type="qualifier", date=date(2026, 9, 1))
    _team_with_school()

    with pytest.raises(FinalizeError, match="already has an event named"):
        finalize_open_event(NewEvent("Invitational 1", date(2026, 10, 3), "invitational"), "gold")
    assert Event.objects.open() is None


def test_finalizes_the_open_event(client):
    event = _open_event()
    _team_with_school()

    assert _finalize(client, open_event=event.pk)["success"]
    assert EventResult.objects.filter(event=event).count() == 1


def test_orange_scorecard_list_breaks_ties_by_pk():
    team = _team_with_school()
    OrangeTeamScore.objects.create(team=team, description="Same", points_awarded=Decimal(5), is_approved=True)
    standings = compute_standings()

    context = build_scorecard_context(standings[0], standings)

    assert context["orange_scores"].query.order_by == ("description", "pk")


def test_inject_scorecard_list_breaks_ties_by_pk():
    team = _team_with_school()
    InjectScore.objects.create(
        team=team, inject_id="a", inject_name="Same", points_awarded=Decimal(5), is_approved=True
    )
    standings = compute_standings()

    with CaptureQueriesContext(connection) as queries:
        build_scorecard_context(standings[0], standings)

    listing = next(q["sql"] for q in queries if q["sql"].startswith('SELECT "inject_grade"."id"'))
    assert listing.endswith('ORDER BY "inject_grade"."inject_name" ASC, "inject_grade"."id" ASC')


def test_service_scorecard_list_breaks_ties_by_pk():
    team = _team_with_school()
    ServiceDetail.objects.create(team=team, service_name="Same", points=Decimal(5))
    standings = compute_standings()

    with CaptureQueriesContext(connection) as queries:
        build_scorecard_context(standings[0], standings)

    listing = next(q["sql"] for q in queries if q["sql"].startswith('SELECT "service_detail"."id"'))
    assert listing.endswith('ORDER BY "service_detail"."service_name" ASC, "service_detail"."id" ASC')


def test_finalize_needs_admin_or_gold_access(create_user_with_groups):
    _season()
    _team_with_school()
    client = Client()
    client.force_login(create_user_with_groups("nobody", []))

    response = client.post(
        reverse("admin_competition_action"),
        {"action": "finalize_event", "event_name": "Inv", "event_date": "2026-10-03", "event_type": "invitational"},
    )

    assert response.status_code == 403
    assert not Event.objects.exists() and not EventResult.objects.exists()


def _page(client):
    return client.get(reverse("admin_competition")).content.decode()


def test_the_page_names_the_season_the_new_event_goes_into(client):
    _season()
    _team_with_school()

    assert "New event in season <strong>2026</strong>" in _page(client)


def test_after_clearing_the_page_points_at_the_next_import_not_finalize(client):
    event = _open_event()
    _team_with_school()
    finalize_open_event(event, "gold")
    clear_event("gold")

    page = _page(client)

    assert "Import the next school list to open an event." in page
    assert "Finalize Results" not in page
