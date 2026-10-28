"""Team password resets, and what unlinking a team member tells Discord."""

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from registration.models import School

from core.authentik_manager import AuthentikManager
from core.authentik_utils import CredentialReset, reset_team_credentials, reset_team_password, sessions_failure_summary
from core.models import DiscordTask
from team.models import DiscordLink, SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def school_info() -> SchoolInfo:
    team = Team.objects.create(team_number=7, team_name="Team 07")
    return SchoolInfo.objects.create(
        team=team, school=School.for_name("School"), contact_email="c@example.com", password="old"
    )


def test_reset_records_the_new_password_on_the_school_info(school_info):
    with patch("core.authentik_manager.AuthentikManager") as manager:
        manager.return_value.reset_blueteam_password.return_value = (True, "")
        password, error = reset_team_password(7)

    assert password and error == ""
    manager.return_value.reset_blueteam_password.assert_called_once_with(7, password)
    school_info.refresh_from_db()
    assert school_info.password == password


def test_failed_reset_records_nothing(school_info):
    with patch("core.authentik_manager.AuthentikManager") as manager:
        manager.return_value.reset_blueteam_password.return_value = (False, "HTTP 500")
        assert reset_team_password(7) == (None, "HTTP 500")

    school_info.refresh_from_db()
    assert school_info.password == "old"


def test_authentik_reset_leaves_the_account_disabled():
    """Outside the competition team accounts stay disabled; a reset must not open them up."""
    manager = AuthentikManager.__new__(AuthentikManager)
    manager.base_url = "https://auth.example"
    manager.client = MagicMock()
    manager.client.get.return_value.is_error = False
    manager.client.get.return_value.json.return_value = {"results": [{"pk": 5, "username": "team07"}]}
    manager.client.post.return_value.is_error = False

    ok, _ = manager.reset_blueteam_password(7, "new-pass")

    assert ok
    manager.client.patch.assert_not_called()


@pytest.mark.parametrize("action", ["unlink_user", "reset"])
def test_web_unlink_queues_a_role_sync_for_the_member(admin_user, action):
    team = Team.objects.create(team_number=8, team_name="Team 08")
    member = User.objects.create(username="member08")
    DiscordLink.objects.create(user=member, discord_id=4242, discord_username="m", team=team, is_active=True)
    client = Client()
    client.force_login(admin_user)

    with patch("core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password="pw")):
        client.post(reverse("admin_team_action", args=[8]), {"action": action, "discord_id": 4242})

    task = DiscordTask.objects.get(task_type="sync_member_roles")
    assert task.payload == {"discord_id": 4242}
    assert not DiscordLink.objects.filter(discord_id=4242, is_active=True).exists()


def test_reset_team_credentials_records_the_password_and_the_sessions_outcome(school_info):
    with patch("core.authentik_manager.AuthentikManager") as manager:
        manager.return_value.reset_team_credentials.return_value = (None, "HTTP 403")
        reset = reset_team_credentials(7)

    assert reset.password and reset.sessions_error == "HTTP 403" and not reset.sessions_ended
    manager.return_value.reset_team_credentials.assert_called_once_with(7, reset.password)
    manager.return_value.close.assert_called_once()
    school_info.refresh_from_db()
    assert school_info.password == reset.password


def test_reset_team_credentials_failure_records_nothing(school_info):
    with patch("core.authentik_manager.AuthentikManager") as manager:
        manager.return_value.reset_team_credentials.return_value = ("User team07 not found", None)
        assert reset_team_credentials(7) == CredentialReset(password=None, error="User team07 not found")

    school_info.refresh_from_db()
    assert school_info.password == "old"


def test_credential_reset_audit_details():
    assert CredentialReset(password="pw").audit_details() == {
        "password_reset": True,
        "sessions_revoked": True,
        "sessions_error": None,
    }
    assert CredentialReset(password=None, error="x").audit_details()["sessions_revoked"] is False


def test_one_reason_shared_by_many_teams_reads_once():
    """A bulk reset where every team fails the same way must stay short (the bot's reply is capped by Discord)."""
    summary = sessions_failure_summary([(f"team{n:02d}", "HTTP 403") for n in range(1, 51)])

    assert summary.count("HTTP 403") == 1
    assert len(summary) < 600


def test_web_bulk_reset_defaults_to_the_active_teams(admin_user):
    """Inactive teams' accounts aren't competing; new passwords for them would only be handed out."""
    for number in range(1, 5):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=number <= 2)
    client = Client()
    client.force_login(admin_user)

    with patch(
        "core.authentik_utils.reset_team_credentials",
        side_effect=lambda n: CredentialReset(password=f"pw-{n}", sessions_error="HTTP 403" if n == 2 else None),
    ) as reset:
        response = client.post(reverse("admin_competition_action"), {"action": "reset_passwords", "team_numbers": ""})

    assert [c.args[0] for c in reset.call_args_list] == [1, 2]
    body = response.json()
    assert body["csv"].splitlines()[1:] == ["team01,pw-1", "team02,pw-2"]
    assert body["message"] == "Reset 2/2 passwords; could not end sessions for team02: HTTP 403"


def test_team_page_password_reset_keeps_discord_links(admin_user):
    """Reset Password changes only the password; Reset Team is the one that unlinks members."""
    from core.models import AuditLog

    team = Team.objects.create(team_number=8, team_name="Team 08")
    member = User.objects.create(username="member08b")
    DiscordLink.objects.create(user=member, discord_id=4343, discord_username="m", team=team, is_active=True)
    client = Client()
    client.force_login(admin_user)

    with patch(
        "core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password="New-Pass-9!")
    ) as reset:
        response = client.post(reverse("admin_team_action", args=[8]), {"action": "reset_password"})

    reset.assert_called_once_with(8)
    body = response.json()
    assert body == {
        "success": True,
        "message": "New password set in Authentik; ended its sessions",
        "password": "New-Pass-9!",
    }
    assert DiscordLink.objects.filter(discord_id=4343, is_active=True).exists()
    assert not DiscordTask.objects.filter(task_type="sync_member_roles").exists()
    assert AuditLog.objects.get(action="team_password_reset").details["sessions_revoked"] is True


def test_team_page_password_reset_reports_a_failure(admin_user):
    Team.objects.create(team_number=8, team_name="Team 08")
    client = Client()
    client.force_login(admin_user)

    with patch(
        "core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password=None, error="HTTP 500")
    ):
        body = client.post(reverse("admin_team_action", args=[8]), {"action": "reset_password"}).json()

    assert body == {"success": False, "message": "Password reset failed: HTTP 500"}
