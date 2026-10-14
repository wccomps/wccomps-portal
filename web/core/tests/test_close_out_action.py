"""The competition page's Close Out Event: typed confirmation, streamed progress, and the module it shows."""

import json
import threading
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse
from registration.models import Event, EventResult, Season
from registration.services import close_out_event, set_team_school
from scoring.models import InjectScore, ServiceScore

from core.models import CompetitionConfig, DiscordTask
from team.models import Team

# The close-out streams from its own thread (core.utils.run_detached), which needs committed rows
pytestmark = pytest.mark.django_db(transaction=True)

BUTTON = '@click="closeOutEvent"'


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _close_out(client, **data):
    response = client.post(reverse("admin_competition_action"), {"action": "close_out_event", **data})
    lines = [json.loads(line) for line in b"".join(response.streaming_content).decode().splitlines()]
    for thread in threading.enumerate():
        if thread.name == "run-detached":
            thread.join()
    return lines


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


def _assert_nothing_happened(event=None):
    assert not EventResult.objects.exists()
    assert not DiscordTask.objects.exists()
    assert ServiceScore.objects.exists()
    if event is not None:
        event.refresh_from_db()
        assert event.is_active


def test_close_out_streams_each_step_and_closes_the_open_event(client):
    event = _open_event()
    _team_with_school()

    lines = _close_out(client, open_event=event.pk, confirm="yolo")

    assert [line.get("step") for line in lines[:-1]] == [
        "Checking, freezing the results, clearing the live data and closing the event",
        "Froze 1 school result(s) and the export archive",
        "Cleared the live data. Deleted: 1 ServiceScore",
        "Queued the Discord cleanup; the bot reports its progress in the ops channel",
    ]
    assert lines[-1] == {"done": True, "success": True, "message": "Closed out Inv."}
    event.refresh_from_db()
    assert not event.is_active and event.is_finalized
    assert DiscordTask.objects.filter(task_type="cleanup_competition").count() == 1


@pytest.mark.parametrize("typed", [None, "", "no"])
def test_close_out_needs_the_typed_confirmation(client, typed):
    event = _open_event()
    _team_with_school()

    lines = _close_out(client, open_event=event.pk, **({} if typed is None else {"confirm": typed}))

    assert lines == [{"done": True, "success": False, "message": 'Type "yolo" to confirm.'}]
    _assert_nothing_happened(event)


def test_refused_while_the_competition_runs(client):
    event = _open_event()
    _team_with_school()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    assert _close_out(client, open_event=event.pk, confirm="yolo")[-1] == {
        "done": True,
        "success": False,
        "message": "Stop the competition first.",
    }
    _assert_nothing_happened(event)


@pytest.mark.parametrize("user", ["nobody", "red"])
def test_close_out_needs_admin_or_gold_access(create_user_with_groups, user):
    event = _open_event()
    _team_with_school()
    client = Client()
    client.force_login(create_user_with_groups(user, ["WCComps_RedTeam"] if user == "red" else []))

    response = client.post(
        reverse("admin_competition_action"), {"action": "close_out_event", "open_event": event.pk, "confirm": "yolo"}
    )

    assert response.status_code == 403
    _assert_nothing_happened(event)


def test_admin_can_close_out(admin_user):
    event = _open_event()
    _team_with_school()
    client = Client()
    client.force_login(admin_user)

    assert _close_out(client, open_event=event.pk, confirm="yolo")[-1]["success"]


def test_no_open_event_needs_a_name(client):
    _season()
    _team_with_school()

    assert _close_out(client, confirm="yolo") == [
        {
            "done": True,
            "success": False,
            "message": "No event is open: give the event a name, a date and a valid type to close out the current "
            "list.",
        }
    ]
    assert not Event.objects.exists()


def test_an_event_name_over_255_characters_is_refused(client):
    _season()
    _team_with_school()

    lines = _close_out(client, confirm="yolo", event_name="x" * 256, event_date="2026-10-03", event_type="invitational")

    assert lines[-1]["message"] == "The event name must be at most 255 characters."
    assert not Event.objects.exists()


def test_unknown_event_type_is_refused(client):
    _season()
    _team_with_school()

    lines = _close_out(client, confirm="yolo", event_name="Inv 1", event_date="2026-10-03", event_type="bogus")

    assert lines[-1]["message"] == "Choose a valid event type."
    assert not Event.objects.exists()


def test_no_open_event_creates_and_closes_it_for_the_current_list(client):
    _season()
    _team_with_school()

    lines = _close_out(
        client, confirm="yolo", event_name="Invitational 1", event_date="2026-10-03", event_type="invitational"
    )

    assert lines[-1] == {"done": True, "success": True, "message": "Closed out Invitational 1."}
    result = EventResult.objects.get()
    assert result.event.name == "Invitational 1"
    assert not result.event.is_active
    assert not ServiceScore.objects.exists()


def test_a_new_event_is_refused_when_an_event_opened_since_the_page_loaded(client):
    _team_with_school()
    event = _open_event()

    lines = _close_out(
        client, confirm="yolo", event_name="Invitational 1", event_date="2026-10-03", event_type="invitational"
    )

    assert lines[-1] == {
        "done": True,
        "success": False,
        "message": "Inv (2026) was opened since the page loaded. Reload the page.",
    }
    _assert_nothing_happened(event)


def _page(client):
    return client.get(reverse("admin_competition")).content.decode()


def test_the_page_offers_close_out_for_the_open_event_once_stopped(client):
    _open_event()
    _team_with_school()

    page = _page(client)

    assert BUTTON in page
    assert "Open event: <strong>Inv</strong>" in page
    assert 'id="close_out_name"' not in page


def test_the_page_hides_close_out_while_the_competition_runs(client):
    _open_event()
    _team_with_school()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    page = _page(client)

    assert BUTTON not in page
    assert "Stop the competition before closing out the event." in page


def test_with_no_event_open_the_page_asks_for_the_event_and_names_the_season(client):
    _season()
    _team_with_school()

    page = _page(client)

    assert BUTTON in page
    assert 'id="close_out_name"' in page
    assert "New event in season <strong>2026</strong>" in page


def test_after_closing_out_the_page_points_at_the_next_import(client):
    event = _open_event()
    _team_with_school()
    close_out_event(event, "gold")

    page = _page(client)

    assert "Import the next school list to open an event." in page
    assert BUTTON not in page


def test_the_pages_no_longer_offer_finalize_clear_or_cleanup(client):
    _open_event()
    _team_with_school()

    page = _page(client) + client.get(reverse("admin_competition_danger")).content.decode()

    for gone in ("finalize_event", "Finalize Results", "clear_event", "Clear Event", "cleanup_competition"):
        assert gone not in page


def test_the_page_shows_unfinished_work_above_the_confirmation(client):
    _open_event()
    team = _team_with_school()
    for inject in ("1", "2", "3"):
        InjectScore.objects.create(team=team, inject_id=inject, inject_name="Memo", points_awarded=Decimal(5))

    page = _page(client)

    assert "3 inject grades are not approved." in page
    assert "Scorecards haven&#x27;t been emailed for this event." in page
    assert page.index("3 inject grades are not approved.") < page.index(BUTTON)


def test_the_page_shows_no_warnings_while_close_out_is_not_offered(client):
    _open_event()
    _team_with_school()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    assert "Unfinished work" not in _page(client)
