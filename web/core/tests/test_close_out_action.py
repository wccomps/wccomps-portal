"""The competition page's Close Out Event: typed confirmation, streamed progress, and the module it shows."""

import json
import threading
from datetime import date
from decimal import Decimal
from unittest.mock import patch

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
        "Closing out: freezing the results, then clearing the live data"
    ]
    assert lines[-1] == {
        "done": True,
        "success": True,
        "message": "Closed out Inv: froze 1 school result(s) and the export archive, deleted 1 ServiceScore, and "
        "queued the Discord cleanup (the bot reports it in the ops channel). Next: Import the next school list to "
        "open an event.",
    }
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
        "message": "Stop the competition first, with Stop Competition on the Competition page.",
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
            "message": "Give the new event a name, a date and a type.",
        }
    ]
    assert not Event.objects.exists()


def test_an_event_name_over_255_characters_is_refused(client):
    _season()
    _team_with_school()

    lines = _close_out(client, confirm="yolo", new_name="x" * 256, new_date="2026-10-03", new_type="invitational")

    assert lines[-1]["message"] == "The event name must be at most 255 characters."
    assert not Event.objects.exists()


def test_unknown_event_type_is_refused(client):
    _season()
    _team_with_school()

    lines = _close_out(client, confirm="yolo", new_name="Inv 1", new_date="2026-10-03", new_type="bogus")

    assert lines[-1]["message"] == "Choose a valid event type."
    assert not Event.objects.exists()


def test_no_open_event_creates_and_closes_it_for_the_current_list(client):
    _season()
    _team_with_school()

    lines = _close_out(
        client, confirm="yolo", new_name="Invitational 1", new_date="2026-10-03", new_type="invitational"
    )

    assert lines[-1]["success"] and lines[-1]["message"].startswith("Closed out Invitational 1:")
    result = EventResult.objects.get()
    assert result.event.name == "Invitational 1"
    assert not result.event.is_active
    assert not ServiceScore.objects.exists()


def test_a_new_event_is_refused_when_an_event_opened_since_the_page_loaded(client):
    _team_with_school()
    event = _open_event()

    lines = _close_out(
        client, confirm="yolo", new_name="Invitational 1", new_date="2026-10-03", new_type="invitational"
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
    assert "Inv (2026) is open and teams have scores." in page
    assert 'id="close_out_name"' not in page


def test_the_page_hides_close_out_while_the_competition_runs(client):
    _open_event()
    _team_with_school()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    page = _page(client)

    assert BUTTON not in page
    assert "Stop the competition first, with Stop Competition on the Competition page." in page


def test_with_no_event_open_the_page_asks_for_the_event_and_names_the_season(client):
    _season()
    _team_with_school()

    page = _page(client)

    assert BUTTON in page
    assert 'id="close_out_name"' in page
    assert "new event in season <strong>2026</strong>" in page


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


def _check(client):
    return client.post(reverse("admin_competition_action"), {"action": "close_out_check"})


def test_the_check_lists_unfinished_work_with_links_and_no_problem(client):
    _open_event()
    team = _team_with_school()
    for inject in ("1", "2", "3"):
        InjectScore.objects.create(team=team, inject_id=inject, inject_name="Memo", points_awarded=Decimal(5))

    assert _check(client).json() == {
        "success": True,
        "problem": "",
        "warnings": [
            {"text": "3 inject grades are not approved.", "url": reverse("scoring:inject_grades_review")},
            {"text": "Scorecards haven't been emailed for this event.", "url": reverse("scoring:email_scorecards")},
        ],
    }


def test_the_check_says_what_would_make_close_out_refuse(client):
    _open_event()
    _team_with_school()
    Team.objects.filter(team_number=1).update(is_active=False)

    assert _check(client).json()["problem"] == "Reactivate Team 1 on the Teams page (or remove its school) first."


def test_a_failing_check_says_so_and_leaves_the_page_working(client):
    _open_event()
    _team_with_school()

    with patch("core.admin_views.competition.close_out_warnings", side_effect=RuntimeError("scoring broke")):
        response = _check(client)
        page = client.get(reverse("admin_competition"))

    assert response.status_code == 500
    assert response.json() == {"error": "Couldn't check unfinished work."}
    assert page.status_code == 200


def test_the_page_load_does_not_build_scorecards(client):
    _open_event()
    _team_with_school()

    with patch("registration.close_out_checks.scorecards_fingerprint") as fingerprint:
        page = _page(client)

    fingerprint.assert_not_called()
    assert BUTTON in page
    assert "Couldn't check unfinished work" in page


def test_the_check_needs_admin_or_gold_access(create_user_with_groups):
    _open_event()
    client = Client()
    client.force_login(create_user_with_groups("nobody", []))

    assert _check(client).status_code == 403


def test_right_after_an_import_the_page_offers_no_close_out(client, gold_team_user):
    from team.models import DiscordLink

    _open_event()
    team = _team_with_school()
    ServiceScore.objects.all().delete()
    DiscordLink.objects.create(team=team, discord_id=7, discord_username="blue", user=gold_team_user)

    page = _page(client)

    assert BUTTON not in page
    assert "No team has scores yet, so there is nothing to close out for Inv (2026)." in page


def test_with_no_season_active_close_out_records_the_scores_in_a_new_season(client):
    Season.objects.create(name="2025", year=2025)
    _team_with_school()

    page = _page(client)
    lines = _close_out(
        client,
        confirm="yolo",
        new_name="Invitational 1",
        new_date="2026-10-03",
        new_type="invitational",
        new_season_name="2026-2027",
        new_season_year="2027",
    )

    assert 'id="close_out_season_name"' in page
    assert lines[-1]["success"], lines
    assert EventResult.objects.get().event.season == Season.objects.get(year=2027, is_active=True)
