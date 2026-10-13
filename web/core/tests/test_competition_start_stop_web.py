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


def test_cleanup_is_queued_for_the_bot(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})

    response = _post(admin_client, "cleanup_competition")

    assert response.json()["success"]
    task = DiscordTask.objects.get(task_type="cleanup_competition")
    assert task.payload["requested_by"]


def test_cleanup_refused_while_running(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    assert _post(admin_client, "cleanup_competition").status_code == 400
    assert not DiscordTask.objects.filter(task_type="cleanup_competition").exists()


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


def _finalized_event(team_number=1):
    from registration.services import finalize_open_event, set_team_school

    from team.models import Team

    event = _open_event()
    team, _ = Team.objects.get_or_create(team_number=team_number, defaults={"team_name": f"Team {team_number}"})
    set_team_school(team, "Example University", contact_email="c@example.edu")
    finalize_open_event(None, "admin")
    return event


def test_clear_needs_a_finalized_event(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    _open_event()

    response = _post(admin_client, "clear_event")

    assert response.status_code == 400
    assert response.json()["error"] == "Finalize this event first."


def test_clear_needs_an_open_event(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})

    response = _post(admin_client, "clear_event")

    assert response.status_code == 400
    assert response.json()["error"] == "No event is open. Finalize the event first."


def test_clear_refused_while_running(admin_client):
    from scoring.models import ServiceScore

    from team.models import Team

    team = Team.objects.create(team_number=9, team_name="Team 9")
    ServiceScore.objects.create(team=team, service_points=100)
    event = _finalized_event(team_number=9)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    response = _post(admin_client, "clear_event")

    assert response.status_code == 400
    assert response.json()["error"] == "Competition must be stopped before clearing"
    assert ServiceScore.objects.exists()
    event.refresh_from_db()
    assert event.is_active


def test_clear_resets_ticket_counters_and_says_so(admin_client):
    from team.models import Team

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    Team.objects.create(team_number=9, team_name="Team 9", ticket_counter=85)
    _finalized_event()

    response = _post(admin_client, "clear_event")

    assert response.json()["message"].startswith("Inv cleared. Deleted: ")
    assert response.json()["message"].endswith("; 1 team ticket counters reset")
    assert Team.objects.get(team_number=9).ticket_counter == 0


def test_clear_resets_the_competition_times(admin_client):
    from django.utils import timezone

    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={"applications_enabled": False, "competition_start_time": now, "competition_end_time": now},
    )
    _finalized_event()

    assert _post(admin_client, "clear_event").json()["success"]

    config = CompetitionConfig.get_config()
    assert config.competition_start_time is None
    assert config.competition_end_time is None


def test_a_refused_clear_keeps_the_competition_times(admin_client):
    from django.utils import timezone

    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={"applications_enabled": False, "competition_start_time": now, "competition_end_time": now},
    )
    _open_event()

    assert _post(admin_client, "clear_event").status_code == 400

    assert CompetitionConfig.get_config().competition_start_time == now


def test_clear_needs_admin_or_gold_access(client, create_user_with_groups):
    from scoring.models import ServiceScore

    from team.models import Team

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    event = _finalized_event()
    ServiceScore.objects.create(team=Team.objects.get(team_number=1), service_points=0)
    client.force_login(create_user_with_groups("nobody", []))

    response = client.post(reverse("admin_competition_action"), {"action": "clear_event"})

    assert response.status_code == 403
    assert ServiceScore.objects.exists()
    event.refresh_from_db()
    assert event.is_active


def test_the_wipe_action_is_gone(admin_client):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})

    response = _post(admin_client, "wipe_competition")

    assert response.status_code == 400
    assert "Unknown action" in response.json()["error"]


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
    """A finalized and cleared event, then a score that arrived after the Clear."""
    from registration.models import Event
    from registration.services import clear_event
    from scoring.models import ServiceScore

    from team.models import Team

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    event = _finalized_event()
    clear_event(event, "admin")
    ServiceScore.objects.create(team=Team.objects.get(team_number=1), service_points=3)
    return Event.objects.get(pk=event.pk)


def test_the_danger_page_offers_leftovers_with_their_counts(admin_client):
    _leftovers()

    page = admin_client.get(reverse("admin_competition_danger")).content.decode()

    assert "Clear Leftover Data" in page
    assert "1 ServiceScore" in page


def test_clearing_leftovers_deletes_them(admin_client):
    from scoring.models import ServiceScore

    _leftovers()

    response = _post(admin_client, "clear_leftover_data")

    assert response.json() == {"success": True, "message": "Leftover data cleared. Deleted: 1 ServiceScore"}
    assert not ServiceScore.objects.exists()


def test_without_a_finalized_event_leftovers_are_hidden_and_refused(admin_client):
    from scoring.models import ServiceScore

    from team.models import Team

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    ServiceScore.objects.create(team=Team.objects.create(team_number=1, team_name="Team 1"), service_points=3)

    page = admin_client.get(reverse("admin_competition_danger")).content.decode()
    response = _post(admin_client, "clear_leftover_data")

    assert "Clear Leftover Data" not in page
    assert response.status_code == 400
    assert response.json()["error"] == "Finalize the event first."
    assert ServiceScore.objects.exists()


def test_clearing_leftovers_needs_admin_or_gold_access(client, create_user_with_groups):
    from scoring.models import ServiceScore

    _leftovers()
    client.force_login(create_user_with_groups("nobody", []))

    response = client.post(reverse("admin_competition_action"), {"action": "clear_leftover_data"})

    assert response.status_code == 403
    assert ServiceScore.objects.exists()


def test_clearing_leftovers_is_refused_while_running(admin_client):
    from scoring.models import ServiceScore

    _leftovers()
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    response = _post(admin_client, "clear_leftover_data")

    assert response.json()["error"] == "Competition must be stopped before clearing"
    assert ServiceScore.objects.exists()


def test_with_leftovers_the_competition_page_points_at_the_danger_page(admin_client):
    _leftovers()

    page = admin_client.get(reverse("admin_competition")).content.decode()

    assert "Live data arrived after the last Clear" in page
    assert "Finalize Results" not in page
