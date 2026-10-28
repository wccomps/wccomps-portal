"""run_competition: the one implementation behind the web page, the slash commands and the timer."""

from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from core.discord_tasks import BroadcastMessage
from core.models import AuditLog, CompetitionConfig, DiscordTask
from core.services.competition import run_competition, run_competition_to_completion
from team.models import MAX_TEAMS, Team, team_username

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _team_accounts_through_the_manager():
    """Route team account switches to a team_account mock on whichever manager the test patched in."""
    with patch(
        "core.services.competition.set_team_account_active",
        side_effect=lambda manager, number, is_active: manager.team_account(team_username(number), is_active=is_active),
    ):
        yield


@pytest.fixture
def authentik() -> Iterator[MagicMock]:
    manager = MagicMock()
    manager.enable_application.return_value = (True, None)
    manager.disable_application.return_value = (True, None)
    manager.team_account.return_value = (True, "")
    with (
        patch("core.authentik_manager.AuthentikManager", return_value=manager),
        patch("core.services.user_groups.refresh_user_groups") as refresh,
        patch("scoring.quotient_sync.sync_quotient_metadata") as sync_metadata,
        patch("scoring.quotient_sync.sync_service_scores") as sync_scores,
    ):
        manager.refresh = refresh
        manager.sync_metadata = sync_metadata
        manager.sync_scores = sync_scores
        yield manager


ACTIVE_TEAMS = [1, 2, 3]


@pytest.fixture(autouse=True)
def teams() -> None:
    """Three teams competing, two left over from an earlier event."""
    for number in range(1, 6):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=number in ACTIVE_TEAMS)


@pytest.fixture
def config() -> CompetitionConfig:
    now = timezone.now()
    config, _ = CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "controlled_applications": ["scoring", "netbird"],
            "applications_enabled": False,
            "competition_start_time": now,
            "competition_end_time": now + timedelta(hours=8),
        },
    )
    return config


def test_start_enables_apps_and_accounts_refreshes_groups_and_audits(authentik, config):
    result = run_competition_to_completion(True, "timer")

    assert result.success
    assert result.apps_ok == ["scoring", "netbird"]
    assert result.accounts_ok == result.accounts_total == len(ACTIVE_TEAMS)
    authentik.refresh.assert_called_once()
    authentik.sync_metadata.assert_called_once_with(None)
    authentik.sync_scores.assert_not_called()
    config.refresh_from_db()
    assert config.applications_enabled
    assert config.competition_start_time is None
    assert config.competition_end_time is not None
    assert AuditLog.objects.get(action="competition_started").admin_user == "timer"


def test_start_enables_only_the_active_teams_accounts(authentik, config):
    """Inactive teams' accounts keep their groups between events; enabling them would let them in."""
    run_competition_to_completion(True, "timer")

    enabled = [c.args[0] for c in authentik.team_account.call_args_list]
    assert enabled == ["team01", "team02", "team03"]
    assert all(c.kwargs == {"is_active": True} for c in authentik.team_account.call_args_list)


def test_stop_disables_every_team_account(authentik, config):
    """Including inactive teams' accounts, so one left enabled is closed too."""
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)

    result = run_competition_to_completion(False, "timer")

    assert authentik.team_account.call_count == result.accounts_total == MAX_TEAMS
    assert "Accounts disabled: 50/50" in result.summary()


def test_stop_syncs_service_scores_and_records_actor_user(authentik, config, django_user_model):
    user = django_user_model.objects.create_user(username="gold_admin")
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)

    steps = list(run_competition(False, "gold_admin"))
    result = run_competition_to_completion(False, "gold_admin")

    assert steps[-1].message == "Quotient service scores synced"
    assert steps[-1].ok is True
    authentik.sync_scores.assert_called_with(user)
    authentik.sync_metadata.assert_not_called()
    assert result.quotient_synced is True
    assert "Quotient service scores: synced" in result.summary()
    audit = AuditLog.objects.filter(action="competition_stopped").order_by("-pk").first()
    assert audit is not None and audit.details["quotient_synced"] is True


def test_stop_records_failed_score_sync_without_aborting(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)
    authentik.sync_scores.side_effect = ValueError("Failed to retrieve service scores from Quotient")

    steps = []
    gen = run_competition(False, "timer")
    while True:
        try:
            steps.append(next(gen))
        except StopIteration as done:
            result = done.value
            break

    assert steps[-1].message == "Quotient score sync failed: Failed to retrieve service scores from Quotient"
    assert steps[-1].ok is False
    assert result.success and not result.has_failures
    assert result.quotient_synced is False
    assert "Quotient service scores: sync failed" in result.summary()
    config.refresh_from_db()
    assert not config.applications_enabled
    assert AuditLog.objects.get(action="competition_stopped").details["quotient_synced"] is False


def test_stop_clears_only_the_end_time(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)

    result = run_competition_to_completion(False, "discord:admin")

    assert result.success and authentik.disable_application.call_count == 2
    config.refresh_from_db()
    assert not config.applications_enabled
    assert config.competition_end_time is None
    assert config.competition_start_time is not None


def test_schedule_edited_during_a_run_survives(authentik, config):
    """The run takes a while; it must not write back a stale copy of the whole config."""
    new_end = timezone.now() + timedelta(days=1)

    def edit_schedule_midway(username: str, is_active: bool) -> tuple[bool, str]:
        CompetitionConfig.objects.filter(pk=1).update(competition_end_time=new_end, max_team_members=7)
        return True, ""

    authentik.team_account.side_effect = edit_schedule_midway
    run_competition_to_completion(True, "timer")

    config.refresh_from_db()
    assert (config.competition_end_time, config.max_team_members) == (new_end, 7)


@pytest.mark.parametrize(("enable", "expected_accounts"), [(True, len(ACTIVE_TEAMS)), (False, MAX_TEAMS)])
def test_progress_counts_every_step(authentik, config, enable, expected_accounts):
    steps = list(run_competition(enable, "web:admin"))
    total = 2 + expected_accounts + 2  # apps, accounts, group refresh, Quotient sync
    assert [s.current for s in steps] == list(range(1, total + 1))
    assert {s.total for s in steps} == {total}


def test_no_controlled_apps_changes_nothing(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(controlled_applications=[])

    result = run_competition_to_completion(True, "timer")

    assert result.error == "No controlled applications configured"
    authentik.team_account.assert_not_called()
    assert not AuditLog.objects.exists()


def test_on_step_runs_after_every_step(authentik, config):
    """The bot beats its liveness heartbeat from here, so a slow Authentik run isn't restarted midway."""
    beats = []

    run_competition_to_completion(True, "timer", on_step=lambda: beats.append(1))

    assert len(beats) == 2 + len(ACTIVE_TEAMS) + 2  # apps, accounts, group refresh, Quotient sync


def _queued_broadcasts() -> list[BroadcastMessage]:
    return [
        payload for task in DiscordTask.objects.all() if isinstance(payload := task.typed_payload(), BroadcastMessage)
    ]


def test_start_queues_the_start_message_to_every_active_team(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(start_message="Default login: admin / changeme")

    result = run_competition_to_completion(True, "timer")

    assert _queued_broadcasts() == [
        BroadcastMessage(target="all-teams", message="Default login: admin / changeme", sender="Black Team")
    ]
    assert "Start message queued" in result.summary()
    assert AuditLog.objects.get(action="competition_started").details["start_message_queued"] is True


def test_start_message_goes_out_once_when_the_start_is_retried(authentik, config):
    """The timer reruns a start that partly failed; teams must not get the credentials again."""
    CompetitionConfig.objects.filter(pk=1).update(start_message="Default login: admin / changeme")

    run_competition_to_completion(True, "timer")
    retry = run_competition_to_completion(True, "timer")

    assert len(_queued_broadcasts()) == 1
    assert not retry.start_message_queued


def test_blank_start_message_sends_nothing(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(start_message="  \n")

    result = run_competition_to_completion(True, "timer")

    assert _queued_broadcasts() == []
    assert not result.start_message_queued


def test_stop_never_sends_the_start_message(authentik, config):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True, start_message="Default login")

    run_competition_to_completion(False, "timer")

    assert _queued_broadcasts() == []


def test_the_last_start_reader_returns_the_accounts_or_none():
    from core.models import AuditLog
    from core.services.competition import COMPETITION_STARTED, last_start_accounts

    assert last_start_accounts() is None
    AuditLog.objects.create(
        action=COMPETITION_STARTED, admin_user="g", target_entity="competition_config", target_id=1, details={}
    )
    assert last_start_accounts() is None
    AuditLog.objects.create(
        action=COMPETITION_STARTED,
        admin_user="g",
        target_entity="competition_config",
        target_id=1,
        details={"accounts_ok": 2, "accounts_failed": 1},
    )
    assert last_start_accounts() == (2, 3)


def test_a_failed_account_step_says_why(authentik, config):
    authentik.team_account.return_value = (False, "User team02 not found")

    steps = list(run_competition(True, "timer"))

    assert any(s.message == "Failed team01: User team02 not found" for s in steps)
