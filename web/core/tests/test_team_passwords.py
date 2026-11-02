"""Team password resets, and what unlinking a team member tells Discord."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from registration.models import School

from core.authentik_manager import SESSIONS_HIDDEN, AuthentikAPIError, AuthentikManager
from core.authentik_utils import (
    Changed,
    FollowupFailed,
    Refused,
    SignedOut,
    Unlinked,
    reset_team_credentials,
    reset_teams_credentials,
    sessions_failure_summary,
    set_team_account_active,
)
from core.models import CredentialsReset, DiscordTask, UserGroups
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
        opened.find_user.return_value = {"pk": 5, "username": "team07", "uid": "sub-07"}
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
    real.client.get.return_value.json.return_value = {"results": [{"pk": 5, "username": "team07", "uid": "sub-07"}]}
    real.client.post.return_value.is_success = True

    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = real
        reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    real.client.patch.assert_not_called()


@pytest.mark.parametrize("action", ["unlink_user", "reset"])
def test_web_unlink_queues_a_role_sync_for_the_member(admin_user, manager, action):
    team = Team.objects.create(team_number=8, team_name="Team 08")
    member = User.objects.create(username="member08")
    DiscordLink.objects.create(user=member, discord_id=4242, discord_username="m", team=team, is_active=True)
    client = Client()
    client.force_login(admin_user)

    client.post(reverse("admin_team_action", args=[8]), {"action": action, "discord_id": 4242})

    task = DiscordTask.objects.get(task_type="sync_member_roles")
    assert task.payload == {"discord_id": 4242}
    assert not DiscordLink.objects.filter(discord_id=4242, is_active=True).exists()


def test_reset_team_credentials_records_the_password_and_the_sessions_outcome(school_info, manager):
    manager.user_sessions.side_effect = [[uuid.UUID(int=1), uuid.UUID(int=2)], [uuid.UUID(int=2)]]
    manager.end_sessions.return_value = "Authentik did not end sessions (HTTP 403)"

    reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    assert reset.sessions_error == "Authentik did not end sessions (HTTP 403)"
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

    assert reset == Refused("Set the password of team07: Server error", sessions_found=1)
    manager.end_sessions.assert_not_called()


def test_uncounted_sessions_are_unknown_not_zero(manager):
    manager.sees_any_sessions.return_value = False

    reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    assert (reset.sessions_found, reset.sessions_error) == (None, SESSIONS_HIDDEN)


def test_a_transient_count_error_does_not_fail_the_end_step_but_tells_ops(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), []]

    reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    assert (reset.sessions_found, reset.sessions_error) == (None, None)
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert note.endswith(
        "ended the team's Authentik sessions, but could not count them first; signed the team account out of the portal"
    )


def test_a_failed_count_asks_whether_the_token_sees_sessions_before_trusting_an_empty_list(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), []]
    manager.sees_any_sessions.return_value = False

    reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed) and reset.sessions_error == SESSIONS_HIDDEN


def test_a_counted_empty_list_is_trusted_after_the_password_change(manager):
    manager.sees_any_sessions.side_effect = [True, False]

    reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    assert (reset.sessions_found, reset.sessions_error) == (0, None)


def test_sessions_listed_before_a_failed_end_are_recorded(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), [uuid.UUID(int=1)]]
    manager.end_sessions.side_effect = AuthentikAPIError("Network error (ConnectError)")

    reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    assert (reset.sessions_listed_after, reset.sessions_error) == (1, "Network error (ConnectError)")


def test_sessions_listed_after_a_failed_count_do_not_pass_for_a_count(manager):
    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), [uuid.UUID(int=1)]]

    reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    assert (reset.sessions_found, reset.sessions_listed_after) == (None, 1)
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert "but could not count them first" in note


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
        "sessions_listed_after": 0,
        "store_error": None,
        "record_error": None,
        "discord_unlinked": 0,
        "followup_error": None,
    }
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert note == (
        "Credentials reset for team 7 by gold-admin: ended the team's Authentik sessions; "
        "signed the team account out of the portal"
    )


def test_reset_team_credentials_failure_keeps_the_old_password(school_info, manager):
    manager.find_user.return_value = None

    assert reset_team_credentials(7, actor="gold-admin") == Refused("User team07 not found")

    school_info.refresh_from_db()
    assert school_info.password == "old"


def test_reset_team_credentials_failed_password_tells_ops(school_info, manager):
    manager.set_password.side_effect = AuthentikAPIError("Set the password of team07: Server error", 500)

    assert reset_team_credentials(7, actor="gold-admin") == Refused(
        "Set the password of team07: Server error", sessions_found=0
    )
    school_info.refresh_from_db()
    assert school_info.password == "old"
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert note == (
        "Credentials reset for team 7 by gold-admin: the password reset failed: Set the password of team07: "
        "Server error"
    )


def test_reset_team_credentials_names_an_unlisted_status(manager):
    """A status without a message of its own still says which it was."""
    response = MagicMock(status_code=400, text='{"password": ["too common"]}', url="https://a/x")
    response.json.return_value = {"password": ["too common"]}
    manager.set_password.side_effect = AuthentikManager._handle_response_error(
        AuthentikManager.__new__(AuthentikManager), response, "Set the password of team07"
    )

    assert reset_team_credentials(7, actor="gold-admin") == Refused(
        "Set the password of team07: HTTP 400", sessions_found=0
    )


def test_reset_team_credentials_network_error_names_the_cause(school_info, manager):
    manager.set_password.side_effect = AuthentikAPIError("Network error (ConnectError)")

    assert reset_team_credentials(7, actor="gold-admin") == Refused("Network error (ConnectError)", sessions_found=0)
    school_info.refresh_from_db()
    assert school_info.password == "old"


def test_bulk_reset_uses_one_manager(manager):
    for number in (1, 2, 3):
        Team.objects.create(team_number=number, team_name=f"Team {number}")
    manager.find_user.side_effect = lambda username: {"pk": 9, "username": username, "uid": f"sub-{username}"}

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
            side_effect=lambda _m, n, _actor, _reason, **_kw: Changed(
                f"pw-{n}", sessions_error="HTTP 403" if n == 2 else None, sessions_found=0, followup=SignedOut(0)
            ),
        ) as reset,
    ):
        response = client.post(reverse("admin_competition_action"), {"action": "reset_passwords", "team_numbers": ""})

    assert [c.args[1] for c in reset.call_args_list] == [1, 2]
    body = response.json()
    assert body["success"] is False, "team02's sessions stayed open"
    assert body["csv"].splitlines()[1:] == ["team01,pw-1", "team02,pw-2"]
    assert body["message"] == (
        "Reset 2/2 passwords; could not end sessions for team02 (HTTP 403), so an open Authentik session still "
        "reaches the team's other Authentik apps"
    )


def test_team_page_password_reset_answers_with_the_new_password_and_the_outcome(admin_user):
    """The shared reset ends the Discord links; the page leaves that to it and reports the outcome."""
    from core.models import AuditLog

    team = Team.objects.create(team_number=8, team_name="Team 08")
    member = User.objects.create(username="member08b")
    DiscordLink.objects.create(user=member, discord_id=4343, discord_username="m", team=team, is_active=True)
    client = Client()
    client.force_login(admin_user)

    with patch(
        "core.admin_views.teams.reset_team_credentials",
        return_value=Changed("New-Pass-9!", sessions_found=0, followup=SignedOut(0)),
    ) as reset:
        response = client.post(reverse("admin_team_action", args=[8]), {"action": "reset_password"})

    assert reset.call_args.args == (8,) and reset.call_args.kwargs["actor"]
    body = response.json()
    assert body == {
        "success": True,
        "message": (
            "New password set in Authentik; ended the team's Authentik sessions; "
            "signed the team account out of the portal"
        ),
        "password": "New-Pass-9!",
        "members_unlinked": True,
    }
    assert DiscordLink.objects.filter(discord_id=4343, is_active=True).exists()
    assert not DiscordTask.objects.filter(task_type="sync_member_roles").exists()
    assert not AuditLog.objects.filter(action="team_password_reset").exists(), "the shared reset writes that row"


def test_team_page_password_reset_reports_a_failure(admin_user):
    Team.objects.create(team_number=8, team_name="Team 08")
    client = Client()
    client.force_login(admin_user)

    with patch("core.admin_views.teams.reset_team_credentials", return_value=Refused("HTTP 500")):
        body = client.post(reverse("admin_team_action", args=[8]), {"action": "reset_password"}).json()

    assert body == {"success": False, "message": "Password reset failed: HTTP 500"}


def test_bulk_reset_writes_its_own_audit_row(manager):
    from core.models import AuditLog

    manager.find_user.side_effect = lambda username: (
        {"pk": 9, "username": username, "uid": f"sub-{username}"} if username != "team02" else None
    )
    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = manager
        reset_teams_credentials([1, 2], actor="gold-admin")

    row = AuditLog.objects.get(action="blueteam_passwords_reset")
    assert row.admin_user == "gold-admin"
    assert row.details["team_numbers"] == [1, 2]
    assert (row.details["total_users"], row.details["success_count"], row.details["failed_count"]) == (2, 1, 1)
    teams = row.details["teams"]
    assert (teams["team01"]["password_reset"], teams["team01"]["sessions_found"]) == (True, 0)
    assert teams["team02"]["password_reset"] is False


def test_bulk_audit_names_the_teams_it_could_not_count(manager):
    from core.models import AuditLog

    manager.user_sessions.side_effect = [AuthentikAPIError("List sessions: Server error", 500), [uuid.UUID(int=1)]]
    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = manager
        reset_teams_credentials([1], actor="gold-admin")

    row = AuditLog.objects.get(action="blueteam_passwords_reset")
    team01 = row.details["teams"]["team01"]
    assert (team01["sessions_found"], team01["sessions_listed_after"]) == (None, 1)
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert note.endswith("; could not count the sessions of team01")


def test_web_bulk_reset_leaves_one_audit_row(admin_user):
    from core.models import AuditLog

    Team.objects.create(team_number=1, team_name="Team 1", is_active=True)
    client = Client()
    client.force_login(admin_user)
    with (
        patch("core.authentik_utils.AuthentikManager"),
        patch(
            "core.authentik_utils._reset_credentials",
            return_value=Changed("pw", sessions_found=0, followup=SignedOut(0)),
        ),
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
        patch("core.authentik_utils._reset_credentials", side_effect=lambda _m, n, _actor, _reason, **_kw: results[n]),
    ):
        return client.post(reverse("admin_competition_action"), {"action": "reset_passwords", "team_numbers": ""})


def test_web_bulk_reset_names_each_failed_team_and_keeps_the_csv(admin_user):
    """A partial failure is an error, and the passwords that did change still arrive."""
    body = _bulk_reset(
        admin_user,
        {
            1: Changed("pw-1", sessions_found=0, followup=SignedOut(0)),
            2: Refused("User team02 not found"),
        },
    ).json()

    assert body["success"] is False
    assert "Reset 1/2 passwords" in body["message"]
    assert "team02: User team02 not found" in body["message"]
    assert body["csv"].splitlines()[1:] == ["team01,pw-1"]


def test_web_bulk_reset_where_every_team_failed_is_an_error(admin_user):
    body = _bulk_reset(admin_user, {1: Refused("Find user team01: HTTP 500")}).json()

    assert body["success"] is False
    assert body["message"].startswith("Reset 0/1 passwords")
    assert body["csv"] == ""


def test_set_team_account_active_switches_the_teams_account(manager):
    set_team_account_active(manager, 7, True)
    manager.set_user_active.assert_called_once_with({"pk": 5, "username": "team07", "uid": "sub-07"}, True)


@pytest.mark.parametrize(("number", "why"), [(7, "User team07 not found"), (0, "Team number must be between 1 and 50")])
def test_set_team_account_active_raises_why_it_could_not(manager, number, why):
    manager.find_user.return_value = None

    with pytest.raises(AuthentikAPIError, match=f"^{why}$"):
        set_team_account_active(manager, number, True)
    manager.set_user_active.assert_not_called()


def test_bulk_reset_tells_ops_once(manager):
    manager.find_user.side_effect = lambda username: (
        {"pk": 9, "username": username, "uid": f"sub-{username}"} if username != "team02" else None
    )
    with patch("core.authentik_utils.AuthentikManager") as cls:
        cls.return_value.__enter__.return_value = manager
        reset_teams_credentials([1, 2], actor="gold-admin")

    notes = [t.payload["message"] for t in DiscordTask.objects.filter(task_type="log_to_channel")]
    assert notes == ["Credentials reset by gold-admin: Reset 1/2 passwords; failed team02: User team02 not found"]


def _signed_in(username: str, sub: str) -> Client:
    account = User.objects.create(username=username)
    UserGroups.objects.create(user=account, authentik_id=sub, groups=[])
    client = Client()
    client.force_login(account)
    return client


@pytest.mark.parametrize("bulk", [False, True])
def test_a_credentials_reset_signs_the_team_account_out_of_the_portal(manager, bulk):
    """Someone signed in with leaked team credentials loses the portal too, not only Authentik."""
    manager.find_user.return_value = {"pk": 5, "username": "team07", "uid": "sub-07"}
    team = _signed_in("team07", "sub-07")
    other = _signed_in("team08", "sub-08")

    if bulk:
        reset_teams_credentials([7], actor="gold-admin")
    else:
        reset_team_credentials(7, actor="gold-admin")

    assert not get_user(team).is_authenticated
    assert get_user(other).is_authenticated


def test_a_renamed_team_account_is_found_by_its_sub(manager):
    manager.find_user.return_value = {"pk": 5, "username": "team07", "uid": "sub-07"}
    team = _signed_in("team07~replaced-1", "sub-07")

    reset_team_credentials(7, actor="gold-admin")

    assert not get_user(team).is_authenticated


def test_a_portal_account_holding_the_username_under_another_sub_stays_signed_in(manager):
    """Accounts are keyed by the Authentik sub only: the username may belong to an identity Authentik has replaced."""
    manager.find_user.return_value = {"pk": 5, "username": "team07", "uid": "sub-07"}
    stale = _signed_in("team07", "sub-old")

    reset_team_credentials(7, actor="gold-admin")

    assert get_user(stale).is_authenticated


def test_a_failed_password_change_leaves_portal_sessions_alone(manager):
    manager.find_user.return_value = {"pk": 5, "username": "team07", "uid": "sub-07"}
    manager.set_password.side_effect = AuthentikAPIError("Set the password of team07: Server error", 500)
    team = _signed_in("team07", "sub-07")

    reset_team_credentials(7, actor="gold-admin")

    assert get_user(team).is_authenticated


def test_the_authentik_sessions_end_before_the_portal_sign_out(manager):
    """A live Authentik session could sign the account straight back into the portal in between, before its sign-in
    was checked against the reset time."""
    team = _signed_in("team07", "sub-07")
    signed_in_when_ending: list[bool] = []
    manager.end_sessions.side_effect = lambda ids: signed_in_when_ending.append(get_user(team).is_authenticated)

    reset_team_credentials(7, actor="gold-admin")

    assert signed_in_when_ending == [True]
    assert not get_user(team).is_authenticated


def test_sessions_that_could_not_end_say_other_authentik_apps_stay_open(manager):
    manager.sees_any_sessions.return_value = False

    reset = reset_team_credentials(7, actor="gold-admin")

    assert "an open Authentik session still reaches the team's other Authentik apps" in reset.describe()
    note = DiscordTask.objects.get(task_type="log_to_channel").payload["message"]
    assert "an open Authentik session still reaches the team's other Authentik apps" in note


def test_a_bulk_reset_keeps_every_password_when_a_portal_sign_out_fails(manager):
    from django.db import DatabaseError

    with patch("core.authentik_utils._end_portal_sessions", side_effect=[DatabaseError("deadlock"), None]):
        result = reset_teams_credentials([1, 2], actor="gold-admin")

    assert [username for _, username, _ in result.password_list] == ["team01", "team02"]
    assert "team01: could not sign the team account out of the portal or end its Discord links: deadlock" in (
        result.summary()
    )
    assert result.resets[0][1].followup == FollowupFailed("deadlock")


@pytest.mark.parametrize("bulk", [False, True])
def test_a_credentials_reset_ends_the_teams_discord_links(manager, bulk):
    """Members relink with the new password, which is what a reset is for."""
    from core.models import AuditLog

    team = Team.objects.create(team_number=7, team_name="Team 07")
    other = Team.objects.create(team_number=8, team_name="Team 08")
    member = User.objects.create(username="member07")
    DiscordLink.objects.create(user=member, discord_id=707, discord_username="m7", team=team, is_active=True)
    DiscordLink.objects.create(user=member, discord_id=808, discord_username="m8", team=other, is_active=True)

    if bulk:
        reset_teams_credentials([7], actor="gold-admin")
    else:
        assert reset_team_credentials(7, actor="gold-admin").unlinked == 1

    assert list(DiscordLink.objects.filter(is_active=True).values_list("discord_id", flat=True)) == [808]
    assert AuditLog.objects.get(action="user_unlinked").details["reason"] == "credentials_reset"
    assert DiscordTask.objects.get(task_type="sync_member_roles").payload == {"discord_id": 707}


def test_a_failed_password_change_keeps_the_discord_links(manager):
    team = Team.objects.create(team_number=7, team_name="Team 07")
    member = User.objects.create(username="member07")
    DiscordLink.objects.create(user=member, discord_id=707, discord_username="m7", team=team, is_active=True)
    manager.set_password.side_effect = AuthentikAPIError("Set the password of team07: Server error", 500)

    reset_team_credentials(7, actor="gold-admin")

    assert DiscordLink.objects.filter(discord_id=707, is_active=True).exists()


def test_a_team_reset_unlinks_once_even_when_the_password_change_fails(admin_user, manager):
    """Reset Team unlinks whatever happens to the password; the reset owns the unlink, so it happens once."""
    from core.models import AuditLog

    team = Team.objects.create(team_number=8, team_name="Team 08")
    member = User.objects.create(username="member08")
    DiscordLink.objects.create(user=member, discord_id=4242, discord_username="m", team=team, is_active=True)
    manager.set_password.side_effect = AuthentikAPIError("Set the password of team08: Server error", 500)
    client = Client()
    client.force_login(admin_user)

    body = client.post(reverse("admin_team_action", args=[8]), {"action": "reset"}).json()

    assert body == {
        "success": False,
        "message": (
            "Team partially reset: the password reset failed: Set the password of team08: Server error; "
            "unlinked 1 Discord member(s)"
        ),
        "members_unlinked": True,
    }
    assert [row.details["reason"] for row in AuditLog.objects.filter(action="user_unlinked")] == ["team_reset"]
    assert AuditLog.objects.get(action="team_reset").details["unlinked_members"] == 1


def test_a_reset_locks_the_team_before_ending_its_links(manager):
    """A link committing while the reset waits is seen and ended."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    Team.objects.create(team_number=7, team_name="Team 07")
    with CaptureQueriesContext(connection) as queries:
        reset_team_credentials(7, actor="gold-admin")

    sql = [q["sql"] for q in queries.captured_queries]
    team_lock = next(i for i, q in enumerate(sql) if 'FROM "team_team"' in q and "FOR NO KEY UPDATE" in q)
    link_read = next(i for i, q in enumerate(sql) if 'FROM "team_discordlink"' in q)
    assert team_lock < link_read


def test_reset_team_unlinks_even_when_the_password_change_fails(manager):
    from core.authentik_utils import reset_team

    team = Team.objects.create(team_number=7, team_name="Team 07")
    member = User.objects.create(username="member07")
    DiscordLink.objects.create(user=member, discord_id=707, discord_username="m7", team=team, is_active=True)
    manager.set_password.side_effect = AuthentikAPIError("Set the password of team07: Server error", 500)

    reset = reset_team(7, actor="gold-admin", recreate_channels=False).credentials

    assert reset == Refused("Set the password of team07: Server error", sessions_found=0, followup=Unlinked(1))
    assert not DiscordTask.objects.filter(task_type="setup_team_infrastructure").exists()
    assert DiscordTask.objects.filter(task_type="log_to_channel").count() == 1


def test_a_database_failure_after_a_password_change_keeps_every_password_and_the_audit_row(manager):
    """Authentik already holds the new password, so the operator must get it whatever the database does next."""
    from django.db import DatabaseError

    from core.models import AuditLog

    with patch("core.authentik_utils._end_portal_sessions", side_effect=[None, DatabaseError("disk full"), None]):
        result = reset_teams_credentials([1, 2, 3], actor="gold-admin")

    assert [username for _, username, _ in result.password_list] == ["team01", "team02", "team03"]
    assert "team02: could not sign the team account out of the portal or end its Discord links: disk full" in (
        result.summary()
    )
    assert AuditLog.objects.get(action="blueteam_passwords_reset").details["success_count"] == 3


def test_the_stored_password_survives_a_later_failure(school_info, manager):
    """Packet emails send the stored copy; it must be the password Authentik now holds."""
    from django.db import DatabaseError

    with patch("core.authentik_utils._end_portal_sessions", side_effect=DatabaseError("disk full")):
        reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    school_info.refresh_from_db()
    assert school_info.password == reset.password
    assert "could not sign the team account out of the portal or end its Discord links: disk full" in reset.describe()


def test_a_failed_store_is_said(school_info, manager):
    from django.db import DatabaseError

    with patch("core.authentik_utils._store_password", side_effect=DatabaseError("disk full")):
        reset = reset_team_credentials(7, actor="gold-admin")

    assert "could not store the new password for packet emails: disk full" in reset.describe()
    assert reset.followup == SignedOut(0)


def test_a_failed_reset_record_is_said(manager):
    from django.db import DatabaseError

    with patch("core.authentik_utils._record_reset", side_effect=DatabaseError("disk full")):
        reset = reset_team_credentials(7, actor="gold-admin")

    assert isinstance(reset, Changed)
    assert not reset.complete
    assert "could not record the reset, so a sign-in from before it can still come back in: disk full" in (
        reset.describe()
    )


def test_a_failed_audit_write_keeps_every_password(admin_user, manager):
    """The record comes after Authentik changed the passwords: failing it must not lose them, nor the CSV."""
    from django.db import DatabaseError

    for number in (1, 2):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=True)
    client = Client()
    client.force_login(admin_user)

    with patch("core.authentik_utils.AuditLog.objects.create", side_effect=DatabaseError("audit table locked")):
        body = client.post(
            reverse("admin_competition_action"), {"action": "reset_passwords", "team_numbers": ""}
        ).json()

    assert len(body["csv"].splitlines()) == 3


def test_a_reset_records_when_it_signed_the_account_out(manager):
    """A sign-in from before then is refused (core.oauth)."""
    _signed_in("team07", "sub-07")

    reset_team_credentials(7, actor="gold-admin")

    assert CredentialsReset.objects.filter(authentik_id="sub-07").exists()


def test_a_reset_records_its_time_for_an_account_never_signed_in_to_the_portal(manager):
    """The usual state at a reset before a competition: a code issued before the reset must still be refused."""
    reset_team_credentials(7, actor="gold-admin")

    assert CredentialsReset.objects.filter(authentik_id="sub-07").exists()


def test_the_reset_time_is_taken_once_the_password_has_changed(manager):
    """A sign-in with the old password while Authentik was changing it must still count as before the reset."""
    changing_at = []
    manager.set_password.side_effect = lambda *args: changing_at.append(timezone.now())

    reset_team_credentials(7, actor="gold-admin")

    assert CredentialsReset.objects.get(authentik_id="sub-07").reset_at >= changing_at[0]


def test_a_reset_that_left_authentik_sessions_open_is_not_reported_as_a_success(admin_user, manager):
    """An open Authentik session still reaches the team's other Authentik apps, so the page keeps the warning up."""
    Team.objects.create(team_number=7, team_name="Team 07")
    manager.sees_any_sessions.return_value = False
    client = Client()
    client.force_login(admin_user)

    body = client.post(reverse("admin_team_action", args=[7]), {"action": "reset_password"}).json()

    assert body["success"] is False and body["password"]
    assert "an open Authentik session still reaches the team's other Authentik apps" in body["message"]


def test_a_team_reset_whose_record_failed_says_the_recreation_was_not_queued(manager):
    """The recreation is queued with the audit row; the reply must not promise it when that rolled back."""
    from django.db import DatabaseError

    from core.authentik_utils import reset_team

    Team.objects.create(team_number=7, team_name="Team 07")
    with patch("core.authentik_utils.AuditLog.objects.create", side_effect=DatabaseError("audit table locked")):
        result = reset_team(7, actor="gold-admin", recreate_channels=True)

    assert isinstance(result.credentials, Changed)
    assert result.recreation_queued is False
    assert not DiscordTask.objects.filter(task_type="setup_team_infrastructure").exists()
