"""Before the start, a packet carrying credentials turns the team account on, so they work when it arrives; the
controlled applications stay closed until the start, and once the event runs or was played a packet leaves the
account alone."""

from dataclasses import replace
from datetime import date
from unittest.mock import patch

import pytest
from registration.models import Event, School, Season
from registration.stage import Stage, event_stage

from core.authentik_manager import AuthentikAPIError
from core.models import AuditLog, CompetitionConfig
from packets.models import Packet, PacketDistribution
from packets.services import PacketDistributionService
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db

DISABLED = {"pk": 5, "username": "team01", "uid": "sub-01", "is_active": False}


@pytest.fixture
def distribution():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True)
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    SchoolInfo.objects.create(
        team=team, school=School.for_name("School One"), contact_email="c@example.edu", password="Stored1!"
    )
    packet = Packet.objects.create(
        title="Packet",
        file_data=b"pdf",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=3,
        uploaded_by="gold",
        status="draft",
        send_via_email=True,
    )
    return PacketDistribution.objects.create(packet=packet, team=team)


def _send(distribution, user=DISABLED, error=None):
    """Send the packet with Authentik answering user for team01 (set_user_active raising error, if given); the mocked
    manager and email class."""
    with (
        patch("packets.services.EmailMultiAlternatives") as email,
        patch("core.authentik_utils.AuthentikManager") as cls,
    ):
        manager = cls.return_value.__enter__.return_value
        manager.find_user.return_value = user
        manager.set_user_active.side_effect = error
        PacketDistributionService().send_packet_email(distribution)
    return manager, email


def test_a_packet_send_turns_a_disabled_team_account_on_and_leaves_the_apps_closed(distribution):
    manager, email = _send(distribution)

    manager.set_user_active.assert_called_once()
    assert manager.set_user_active.call_args.args[1] is True
    manager.set_application_enabled.assert_not_called()
    email.return_value.send.assert_called_once()
    row = AuditLog.objects.get(action="team_account_enabled")
    assert (row.admin_user, row.target_id, row.details) == ("packet distribution", 1, {"enabled": True, "error": None})


def test_an_already_active_account_is_left_alone(distribution):
    manager, _ = _send(distribution, {**DISABLED, "is_active": True})

    manager.set_user_active.assert_not_called()
    assert not AuditLog.objects.filter(action="team_account_enabled").exists()


def test_while_the_competition_runs_a_packet_leaves_the_account_alone(distribution):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    manager, email = _send(distribution)

    manager.find_user.assert_not_called()
    email.return_value.send.assert_called_once()


def test_once_the_event_was_played_a_resend_leaves_the_account_off(distribution):
    """After the stop, a resend before Close Out mustn't turn the account back on."""
    played = replace(event_stage(), stage=Stage.PLAYED, running=False)

    with patch("packets.services.event_stage", return_value=played):
        manager, email = _send(distribution)

    manager.find_user.assert_not_called()
    email.return_value.send.assert_called_once()


def test_after_a_start_and_an_early_stop_with_no_scores_a_resend_leaves_the_account_off(distribution):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"started_this_window": True})

    manager, _ = _send(distribution)

    manager.find_user.assert_not_called()


def test_a_team_that_isnt_active_keeps_its_account_off(distribution):
    Team.objects.filter(team_number=1).update(is_active=False)

    manager, _ = _send(distribution)

    manager.find_user.assert_not_called()


def test_a_failure_to_turn_it_on_is_audited_and_the_email_still_goes(distribution):
    _, email = _send(distribution, error=AuthentikAPIError("Set team01 active=True: Authentik server error"))

    email.return_value.send.assert_called_once()
    row = AuditLog.objects.get(action="team_account_enabled")
    assert row.details == {"enabled": False, "error": "Set team01 active=True: Authentik server error"}
