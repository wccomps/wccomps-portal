"""Starting and stopping from the ops page streams the shared service and tells Discord."""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from core.models import CompetitionConfig, DiscordTask

# The run streams from its own thread (core.utils.run_detached), which needs committed rows
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


def _post(client, action, **data):
    return client.post(reverse("admin_competition_action"), {"action": action, **data})


def test_start_streams_progress_and_posts_to_ops(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"controlled_applications": ["scoring"]})
    manager = MagicMock()
    manager.enable_application.return_value = (True, None)
    manager.toggle_user.return_value = (True, "")

    with (
        patch("core.authentik_manager.AuthentikManager", return_value=manager),
        patch("core.services.user_groups.refresh_user_groups") as refresh,
        patch("scoring.quotient_sync.sync_quotient_metadata"),
    ):
        response = _post(admin_client, "start_competition")
        lines = [json.loads(line) for line in b"".join(response.streaming_content).decode().splitlines()]

    assert lines[-1]["done"] and lines[-1]["success"]
    refresh.assert_called_once()
    assert CompetitionConfig.get_config().applications_enabled
    assert "Competition Started" in DiscordTask.objects.get(task_type="log_to_channel").payload["message"]


def test_setting_a_start_time_mid_competition_keeps_it_running(admin_client):
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"controlled_applications": ["scoring"], "applications_enabled": True}
    )

    _post(admin_client, "set_start_time", datetime="2030-01-01T09:00", timezone="UTC")

    config = CompetitionConfig.get_config()
    assert config.applications_enabled
    assert config.competition_start_time is not None


def _join_detached_runs() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name == "run-detached":
            thread.join(timeout=10)


def test_start_finishes_after_the_browser_disconnects(admin_client):
    """A closed tab or a proxy timeout used to stop the start partway: some accounts enabled, state not saved."""
    import threading

    from team.models import Team

    for number in (1, 2, 3):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=True)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"controlled_applications": ["scoring"]})
    browser_gone = threading.Event()
    manager = MagicMock()
    manager.enable_application.return_value = (True, None)

    def toggle_once_the_browser_is_gone(username: str, is_active: bool) -> tuple[bool, str]:
        browser_gone.wait(timeout=10)
        return True, ""

    manager.toggle_user.side_effect = toggle_once_the_browser_is_gone

    with (
        patch("core.authentik_manager.AuthentikManager", return_value=manager),
        patch("core.services.user_groups.refresh_user_groups"),
        patch("scoring.quotient_sync.sync_quotient_metadata"),
    ):
        response = _post(admin_client, "start_competition")
        first = json.loads(next(iter(response.streaming_content)))
        response.close()
        browser_gone.set()
        _join_detached_runs()

    assert first["step"] == "Enabled scoring"
    assert [c.args[0] for c in manager.toggle_user.call_args_list] == ["team01", "team02", "team03"]
    assert CompetitionConfig.get_config().applications_enabled


def _open_event():
    from datetime import date

    from registration.models import Event, Season

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


def _team_with_school(team_number=1):
    from registration.services import set_team_school

    from team.models import Team

    team, _ = Team.objects.get_or_create(team_number=team_number, defaults={"team_name": f"Team {team_number}"})
    set_team_school(team, "Example University", contact_email="c@example.edu")
    return team


@pytest.mark.parametrize("action", ["wipe_competition", "finalize_event", "clear_event", "cleanup_competition"])
def test_the_replaced_actions_are_gone(admin_client, action):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    _open_event()
    _team_with_school()

    response = _post(admin_client, action, confirm="yolo")

    assert response.status_code == 400
    assert "Unknown action" in response.json()["error"]
    assert not DiscordTask.objects.exists()


def test_a_partial_start_is_reported_incomplete_not_success(admin_client):
    """Success made the page reload after 2 s, hiding which apps or accounts didn't change."""
    from team.models import Team

    Team.objects.create(team_number=1, team_name="Team 1", is_active=True)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"controlled_applications": ["scoring"]})
    manager = MagicMock()
    manager.enable_application.return_value = (False, "HTTP 502")
    manager.toggle_user.return_value = (True, "")

    with (
        patch("core.authentik_manager.AuthentikManager", return_value=manager),
        patch("core.services.user_groups.refresh_user_groups"),
        patch("scoring.quotient_sync.sync_quotient_metadata"),
    ):
        response = _post(admin_client, "start_competition")
        done = [json.loads(line) for line in b"".join(response.streaming_content).decode().splitlines()][-1]

    assert done["success"] is False
    assert done["message"].startswith("Incomplete: run Start again.")
    assert "✗ scoring: HTTP 502" in done["message"]
    log = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert log.startswith("**Competition Started (incomplete)**")


def _leftovers():
    """A closed out event, then a score that arrived after the close-out."""
    from registration.models import Event
    from registration.services import close_out_event
    from scoring.models import ServiceScore

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    event = _open_event()
    team = _team_with_school()
    ServiceScore.objects.create(team=team, service_points=5)
    close_out_event(event, "admin")
    ServiceScore.objects.create(team=team, service_points=3)
    return Event.objects.get(pk=event.pk)


def test_the_danger_page_offers_leftovers_with_their_counts(admin_client):
    _leftovers()

    page = " ".join(admin_client.get(reverse("admin_competition_danger")).content.decode().split())

    assert "Clear Leftover Data" in page
    assert "1 ServiceScore" in page
    assert "not recorded at any event yet" in page
    assert "It is not archived" in page


def test_clearing_leftovers_deletes_them(admin_client):
    from scoring.models import ServiceScore

    _leftovers()

    response = _post(admin_client, "clear_leftover_data", confirm="YOLO")

    assert response.json() == {"success": True, "message": "Leftover data cleared. Deleted: 1 ServiceScore"}
    assert not ServiceScore.objects.exists()


def test_without_a_finalized_event_leftovers_are_hidden_and_refused(admin_client):
    from scoring.models import ServiceScore

    from team.models import Team

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    ServiceScore.objects.create(team=Team.objects.create(team_number=1, team_name="Team 1"), service_points=3)

    page = admin_client.get(reverse("admin_competition_danger")).content.decode()
    response = _post(admin_client, "clear_leftover_data", confirm="YOLO")

    assert "Clear Leftover Data" not in page
    assert response.status_code == 400
    assert response.json()["error"] == (
        "These scores are not recorded at any event yet and no event was ever closed out: record them with "
        "Close Out Event."
    )
    assert ServiceScore.objects.exists()


def test_clearing_leftovers_needs_admin_or_gold_access(client, create_user_with_groups):
    from scoring.models import ServiceScore

    _leftovers()
    client.force_login(create_user_with_groups("nobody", []))

    response = client.post(reverse("admin_competition_action"), {"action": "clear_leftover_data", "confirm": "yolo"})

    assert response.status_code == 403
    assert ServiceScore.objects.exists()


def test_clearing_leftovers_is_refused_while_running(admin_client):
    from scoring.models import ServiceScore

    _leftovers()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    response = _post(admin_client, "clear_leftover_data", confirm="YOLO")

    assert response.json()["error"] == "Competition must be stopped before clearing"
    assert ServiceScore.objects.exists()


def test_scores_after_a_close_out_can_still_be_recorded_with_close_out(admin_client):
    """An event that ran without an import after the first close-out is not leftovers to delete unrecorded."""
    _leftovers()

    page = admin_client.get(reverse("admin_competition")).content.decode()

    assert "not recorded at any event yet" in page
    assert '@click="closeOutEvent"' in page


def test_live_data_without_scores_points_at_the_danger_page(admin_client, admin_user):
    from team.models import DiscordLink

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    team = _team_with_school()
    DiscordLink.objects.create(team=team, discord_id=1, discord_username="blue", user=admin_user)

    page = admin_client.get(reverse("admin_competition")).content.decode()

    assert "Clear leftover data on the competition Danger Zone page" in page
    assert '@click="closeOutEvent"' not in page


@pytest.mark.parametrize("typed", [None, "", "no"])
def test_clearing_leftovers_needs_the_typed_confirmation(admin_client, typed):
    from scoring.models import ServiceScore

    _leftovers()

    response = _post(admin_client, "clear_leftover_data", **({} if typed is None else {"confirm": typed}))

    assert response.status_code == 400
    assert response.json()["error"] == 'Type "yolo" to confirm.'
    assert ServiceScore.objects.exists()


def test_clearing_leftovers_keeps_the_schedule(admin_client):
    from django.utils import timezone

    _leftovers()
    now = timezone.now()
    CompetitionConfig.objects.filter(pk=1).update(competition_start_time=now, competition_end_time=now)

    assert _post(admin_client, "clear_leftover_data", confirm="yolo").json()["success"]

    config = CompetitionConfig.get_config()
    assert (config.competition_start_time, config.competition_end_time) == (now, now)


def _scoreless_open_event():
    event = _open_event()
    _team_with_school()
    return event


def test_the_danger_page_offers_cancel_only_for_an_open_event_with_no_scores(admin_client):
    from scoring.models import ServiceScore

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    _scoreless_open_event()
    offered = admin_client.get(reverse("admin_competition_danger")).content.decode()
    ServiceScore.objects.create(team_id=_team_with_school().pk, service_points=1)
    scored = admin_client.get(reverse("admin_competition_danger")).content.decode()

    assert '@click.prevent="guardedCancelEvent"' in offered
    assert '@click.prevent="guardedCancelEvent"' not in scored


def test_cancelling_from_the_danger_page(admin_client):
    from core.models import AuditLog

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    event = _scoreless_open_event()

    response = _post(admin_client, "cancel_event", open_event=event.pk, confirm="yolo")

    assert response.json() == {"success": True, "message": "Inv cancelled. The school list stays."}
    event.refresh_from_db()
    assert not event.is_active
    assert AuditLog.objects.filter(action="event_cancelled").exists()


@pytest.mark.parametrize("typed", [None, "", "no"])
def test_cancelling_needs_the_typed_confirmation(admin_client, typed):
    event = _scoreless_open_event()

    response = _post(admin_client, "cancel_event", open_event=event.pk, **({} if typed is None else {"confirm": typed}))

    assert response.status_code == 400
    assert response.json()["error"] == 'Type "yolo" to confirm.'
    event.refresh_from_db()
    assert event.is_active


def test_cancelling_needs_admin_or_gold_access(client, create_user_with_groups):
    event = _scoreless_open_event()
    client.force_login(create_user_with_groups("nobody", []))

    response = client.post(
        reverse("admin_competition_action"), {"action": "cancel_event", "open_event": event.pk, "confirm": "yolo"}
    )

    assert response.status_code == 403
    event.refresh_from_db()
    assert event.is_active
