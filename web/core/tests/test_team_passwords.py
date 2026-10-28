"""Team password resets, and what unlinking a team member tells Discord."""

import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from registration.models import School

from core.authentik_manager import SESSIONS_HIDDEN, AuthentikAPIError, AuthentikManager
from core.authentik_utils import (
    CredentialReset,
    reset_team_credentials,
    reset_teams_credentials,
    sessions_failure_summary,
    set_team_account_active,
)
from core.models import DiscordTask
from team.models import DiscordLink, SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def school_info() -> SchoolInfo:
    team = Team.objects.create(team_number=7, team_name="Team 07")
    return SchoolInfo.objects.create(
        team=team, school=School.for_name("School"), contact_email="c@example.com", password="old"
    )


@pytest.fixture
def manager():
    """The AuthentikManager the reset helpers open, finding team07's account."""
    with patch("core.authentik_utils.AuthentikManager") as cls:
        opened = cls.return_value.__enter__.return_value
        opened.find_user.return_value = {"pk": 5, "username": "team07"}
        opened.user_sessions.return_value = []
        opened.sees_any_sessions.return_value = True
        opened.end_sessions.return_value = None
        yield opened


def test_authentik_reset_leaves_the_account_disabled():
    """Outside the competition team accounts stay disabled; a reset must not open them up."""
    real = AuthentikManager.__new__(AuthentikManager)
    real.base_url = "https://auth.example"
    real.client = MagicMock()
    real.client.get.return_value.is_success = True
    real.client.get.return_value.json.return_value = {"results": [{"pk": 5, "username": "team07"}]}
    real.client.post.return_value.is_success = True

    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = real
        reset = reset_team_credentials(7, actor="gold-admin")

    assert reset.password
    real.client.patch.assert_not_called()


@pytest.mark.parametrize("action", ["unlink_user", "reset"])
def test_web_unlink_queues_a_role_sync_for_the_member(admin_user, action):
    team = Team.objects.create(team_number=8, team_name="Team 08")
    member = User.objects.create(username="member08")
    DiscordLink.objects.create(user=member, discord_id=4242, discord_username="m", team=team, is_active=True)
    client = Client()
    client.force_login(admin_user)

    with patch(
        "core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password="pw", sessions_found=0)
    ):
        client.post(reverse("admin_team_action", args=[8]), {"action": action, "discord_id": 4242})

    task = DiscordTask.objects.get(task_type="sync_member_roles")
    assert task.payload == {"discord_id": 4242}
    assert not DiscordLink.objects.filter(discord_id=4242, is_active=True).exists()


def test_reset_team_credentials_records_the_password_and_the_sessions_outcome(school_info, manager):
    manager.user_sessions.side_effect = [[uuid.UUID(int=1), uuid.UUID(int=2)], [uuid.UUID(int=2)]]
    manager.end_sessions.return_value = "Authentik did not end sessions (HTTP 403)"

    reset = reset_team_credentials(7, actor="gold-admin")

    assert reset.password and reset.sessions_error == "Authentik did not end sessions (HTTP 403)"
    assert reset.sessions_found == 2
    manager.end_sessions.assert_called_once_with([uuid.UUID(int=2)])
    school_info.refresh_from_db()
    assert school_info.password == reset.password


def test_reset_counts_sessions_before_the_password_changes_and_ends_them_after(manager):
    calls: list[str] = []
    manager.user_sessions.side_effect = lambda user: calls.append("list") or []
    manager.set_password.side_effect = lambda user, password: calls.append("set")
    manager.end_sessions.side_effect = lambda ids: calls.append("end")

    reset_team_credentials(7, actor="gold-admin")

    assert calls == ["list", "set", "list", "end"]


def test_failed_password_keeps_the_count_and_ends_nothing(manager):
    """Signing a team out without changing its password helps nobody, and the count belongs in the audit row."""
    manager.user_sessions.return_value = [uuid.UUID(int=1)]
    manager.set_password.side_effect = AuthentikAPIError("Set the password of team07: Server error", 500)

    reset = reset_team_credentials(7, actor="gold-admin")

    assert (reset.password, reset.sessions_found) == (None, 1)
    manager.end_sessions.assert_not_called()


def test_uncounted_sessions_are_unknown_not_zero(manager):
    manager.sees_any_sessions.return_value = False

    reset = reset_team_credentials(7, actor="gold-admin")

    assert reset.sessions_found is None
    assert reset.sessions_error == SESSIONS_HIDDEN


def test_a_transient_count_error_does_not_fail_the_end_step_but_tells_ops(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), []]

    reset = reset_team_credentials(7, actor="gold-admin")

    assert (reset.sessions_found, reset.sessions_error) == (None, None)
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert note.endswith("ended the team's Authentik sessions, but could not count them first")


def test_a_failed_count_asks_whether_the_token_sees_sessions_before_trusting_an_empty_list(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), []]
    manager.sees_any_sessions.return_value = False

    reset = reset_team_credentials(7, actor="gold-admin")

    assert reset.sessions_error == SESSIONS_HIDDEN


def test_a_counted_empty_list_is_trusted_after_the_password_change(manager):
    """Authentik may end the only session the token could see when the password changes."""
    manager.sees_any_sessions.side_effect = [True, False]

    reset = reset_team_credentials(7, actor="gold-admin")

    assert (reset.sessions_found, reset.sessions_error) == (0, None)


def test_sessions_listed_before_a_failed_end_still_count(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), [uuid.UUID(int=1)]]
    manager.end_sessions.side_effect = httpx.ConnectError("refused")

    reset = reset_team_credentials(7, actor="gold-admin")

    assert (reset.sessions_found, reset.sessions_error) == (1, "Network error (ConnectError)")


def test_sessions_the_end_step_finds_count_when_the_first_count_failed(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), [uuid.UUID(int=1)]]

    reset = reset_team_credentials(7, actor="gold-admin")

    assert reset.sessions_found == 1


def test_reset_team_credentials_audits_and_tells_ops(manager):
    from core.models import AuditLog

    manager.user_sessions.side_effect = [[uuid.UUID(int=1)], []]

    reset_team_credentials(7, actor="gold-admin")

    row = AuditLog.objects.get(action="team_password_reset")
    assert (row.admin_user, row.target_id) == ("gold-admin", 7)
    assert row.details == {
        "password_reset": True,
        "sessions_revoked": True,
        "sessions_error": None,
        "sessions_found": 1,
    }
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert note == "Credentials reset for team 7 by gold-admin: ended the team's Authentik sessions"


def test_reset_team_credentials_failure_keeps_the_old_password(school_info, manager):
    manager.find_user.return_value = None

    assert reset_team_credentials(7, actor="gold-admin") == CredentialReset(
        password=None, error="User team07 not found", sessions_found=None
    )

    school_info.refresh_from_db()
    assert school_info.password == "old"


def test_reset_team_credentials_failed_password_tells_ops(school_info, manager):
    manager.set_password.side_effect = AuthentikAPIError("Set the password of team07: Server error", 500)

    assert reset_team_credentials(7, actor="gold-admin") == CredentialReset(
        password=None, error="Set the password of team07: Server error", sessions_found=0
    )
    school_info.refresh_from_db()
    assert school_info.password == "old"
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert note == "Credentials reset for team 7 by gold-admin failed: Set the password of team07: Server error"


def test_reset_team_credentials_names_an_unlisted_status(manager):
    """A status without a message of its own still says which it was."""
    response = MagicMock(status_code=400, text='{"password": ["too common"]}', url="https://a/x")
    response.json.return_value = {"password": ["too common"]}
    manager.set_password.side_effect = AuthentikManager._handle_response_error(
        AuthentikManager.__new__(AuthentikManager), response, "Set the password of team07"
    )

    assert reset_team_credentials(7, actor="gold-admin").error == "Set the password of team07: HTTP 400"


def test_reset_team_credentials_network_error_names_the_cause(school_info, manager):
    manager.set_password.side_effect = httpx.ConnectError("refused")

    assert reset_team_credentials(7, actor="gold-admin") == CredentialReset(
        password=None, error="Network error (ConnectError)", sessions_found=0
    )
    school_info.refresh_from_db()
    assert school_info.password == "old"


def test_bulk_reset_uses_one_manager(manager):
    for number in (1, 2, 3):
        Team.objects.create(team_number=number, team_name=f"Team {number}")
    manager.find_user.side_effect = lambda username: {"pk": 9, "username": username}

    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = manager
        result = reset_teams_credentials([1, 2, 3], actor="gold-admin")

    assert cls.call_count == 1
    assert [username for _num, username, _pw in result.password_list] == ["team01", "team02", "team03"]


def test_one_reason_shared_by_many_teams_reads_once():
    """A bulk reset where every team fails the same way reads one reason, not fifty copies of it."""
    summary = sessions_failure_summary([(f"team{n:02d}", "HTTP 403") for n in range(1, 51)])

    assert summary.count("HTTP 403") == 1
    assert len(summary) < 600


def test_web_bulk_reset_defaults_to_the_active_teams(admin_user):
    """Inactive teams' accounts aren't competing; new passwords for them would only be handed out."""
    for number in range(1, 5):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=number <= 2)
    client = Client()
    client.force_login(admin_user)

    with (
        patch("core.authentik_utils.AuthentikManager"),
        patch(
            "core.authentik_utils._reset_credentials",
            side_effect=lambda _m, n: CredentialReset(
                password=f"pw-{n}", sessions_error="HTTP 403" if n == 2 else None
            ),
        ) as reset,
    ):
        response = client.post(reverse("admin_competition_action"), {"action": "reset_passwords", "team_numbers": ""})

    assert [c.args[1] for c in reset.call_args_list] == [1, 2]
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
        "core.admin_views.teams.reset_team_credentials",
        return_value=CredentialReset(password="New-Pass-9!", sessions_found=0),
    ) as reset:
        response = client.post(reverse("admin_team_action", args=[8]), {"action": "reset_password"})

    assert reset.call_args.args == (8,) and reset.call_args.kwargs["actor"]
    body = response.json()
    assert body == {
        "success": True,
        "message": "New password set in Authentik; ended the team's Authentik sessions",
        "password": "New-Pass-9!",
    }
    assert DiscordLink.objects.filter(discord_id=4343, is_active=True).exists()
    assert not DiscordTask.objects.filter(task_type="sync_member_roles").exists()
    assert not AuditLog.objects.filter(action="team_password_reset").exists(), "the shared reset writes that row"


def test_team_page_password_reset_reports_a_failure(admin_user):
    Team.objects.create(team_number=8, team_name="Team 08")
    client = Client()
    client.force_login(admin_user)

    with patch(
        "core.admin_views.teams.reset_team_credentials", return_value=CredentialReset(password=None, error="HTTP 500")
    ):
        body = client.post(reverse("admin_team_action", args=[8]), {"action": "reset_password"}).json()

    assert body == {"success": False, "message": "Password reset failed: HTTP 500"}


def test_bulk_reset_writes_its_own_audit_row(manager):
    from core.models import AuditLog

    manager.find_user.side_effect = lambda username: {"pk": 9, "username": username} if username != "team02" else None
    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = manager
        reset_teams_credentials([1, 2], actor="gold-admin")

    row = AuditLog.objects.get(action="blueteam_passwords_reset")
    assert row.admin_user == "gold-admin"
    assert row.details["team_numbers"] == [1, 2]
    assert row.details["total_users"] == 2
    assert row.details["success_count"] == 1
    assert row.details["failed_resets"] == ["team02: User team02 not found"]
    assert (row.details["sessions_found"], row.details["sessions_uncounted"]) == (0, [])


def test_bulk_audit_names_the_teams_it_could_not_count(manager):
    from core.models import AuditLog

    manager.user_sessions.side_effect = AuthentikAPIError("List sessions: Server error", 500)
    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = manager
        reset_teams_credentials([1], actor="gold-admin")

    row = AuditLog.objects.get(action="blueteam_passwords_reset")
    assert (row.details["sessions_found"], row.details["sessions_uncounted"]) == (0, ["team01"])


def test_web_bulk_reset_leaves_one_audit_row(admin_user):
    from core.models import AuditLog

    Team.objects.create(team_number=1, team_name="Team 1", is_active=True)
    client = Client()
    client.force_login(admin_user)
    with (
        patch("core.authentik_utils.AuthentikManager"),
        patch("core.authentik_utils._reset_credentials", return_value=CredentialReset(password="pw", sessions_found=0)),
    ):
        client.post(reverse("admin_competition_action"), {"action": "reset_passwords", "team_numbers": ""})

    assert AuditLog.objects.filter(action="blueteam_passwords_reset").count() == 1


def _bulk_reset(admin_user, results):
    for number in results:
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=True)
    client = Client()
    client.force_login(admin_user)
    with (
        patch("core.authentik_utils.AuthentikManager"),
        patch("core.authentik_utils._reset_credentials", side_effect=lambda _m, n: results[n]),
    ):
        return client.post(reverse("admin_competition_action"), {"action": "reset_passwords", "team_numbers": ""})


def test_web_bulk_reset_names_each_failed_team_and_keeps_the_csv(admin_user):
    """A partial failure is an error, and the passwords that did change still arrive."""
    body = _bulk_reset(
        admin_user,
        {
            1: CredentialReset(password="pw-1", sessions_found=0),
            2: CredentialReset(password=None, error="User team02 not found"),
        },
    ).json()

    assert body["success"] is False
    assert "Reset 1/2 passwords" in body["message"]
    assert "team02: User team02 not found" in body["message"]
    assert body["csv"].splitlines()[1:] == ["team01,pw-1"]


def test_web_bulk_reset_where_every_team_failed_is_an_error(admin_user):
    body = _bulk_reset(admin_user, {1: CredentialReset(password=None, error="Find user team01: HTTP 500")}).json()

    assert body["success"] is False
    assert body["message"].startswith("Reset 0/1 passwords")
    assert body["csv"] == ""


def test_set_team_account_active_switches_the_teams_account(manager):
    assert set_team_account_active(manager, 7, True) == (True, "")
    manager.set_user_active.assert_called_once_with({"pk": 5, "username": "team07"}, True)


def test_bulk_reset_tells_ops_once(manager):
    manager.find_user.side_effect = lambda username: {"pk": 9, "username": username} if username != "team02" else None
    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = manager
        reset_teams_credentials([1, 2], actor="gold-admin")

    notes = [t.payload["message"] for t in DiscordTask.objects.filter(task_type="log_to_channel")]
    assert notes == ["Credentials reset by gold-admin: Reset 1/2 passwords; failed: team02: User team02 not found"]
