"""Authentik group names, team accounts and the team cap come from settings; their defaults are WRCCDC's names."""

import importlib
from unittest.mock import patch

import pytest
from django.urls import reverse

from core import permission_constants
from core.admin_views import readiness
from core.authentik_manager import AuthentikGroupEntry, AuthentikUserEntry
from core.team_accounts import TeamAccounts, account_pattern, single_account, team_accounts
from team.models import MAX_TEAMS, Team, team_group_name, team_username

TODAY_PERMISSION_MAP = {  # permission_constants.py at f207dc23
    "admin": ["WCComps_Discord_Admin"],
    "ticketing_admin": ["WCComps_Ticketing_Admin", "WCComps_Discord_Admin"],
    "ticketing_support": ["WCComps_Ticketing_Support", "WCComps_Ticketing_Admin", "WCComps_Discord_Admin"],
    "gold_team": ["WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "white_team": ["WCComps_WhiteTeam", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "orange_team": ["WCComps_OrangeTeam", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "white_team_lead": ["WCComps_WhiteTeam_Lead", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "orange_team_lead": ["WCComps_OrangeTeam_Lead", "WCComps_GoldTeam", "WCComps_Discord_Admin"],
    "red_team": ["WCComps_RedTeam", "WCComps_Discord_Admin"],
    "blue_team": ["WCComps_GoldTeam", "WCComps_Discord_Admin"],
}

GROUP_3: AuthentikGroupEntry = {"pk": "g3", "name": "WCComps_BlueTeam03", "parents": []}


def user(username: str, *group_pks: str) -> AuthentikUserEntry:
    return {"pk": 0, "username": username, "uid": username, "is_active": False, "groups": list(group_pks)}


@pytest.fixture
def reload_permissions(settings):
    """Reload permission_constants under the test's settings, then put back the map built from the defaults. This
    teardown runs before the settings fixture restores them, so reloading here would keep the test's names."""
    original = permission_constants.PERMISSION_MAP
    yield lambda: importlib.reload(permission_constants)
    permission_constants.PERMISSION_MAP = original


def test_defaults_match_today():
    assert permission_constants.PERMISSION_MAP == TODAY_PERMISSION_MAP
    assert team_username(7) == "team07"
    assert team_group_name(7) == "WCComps_BlueTeam07"
    assert MAX_TEAMS == 50
    assert single_account()


def test_prefix_changes_every_group(settings, reload_permissions):
    settings.PORTAL_GROUP_PREFIX, settings.PORTAL_ADMIN_GROUP = "Region_", "Region_Admin"
    reload_permissions()
    assert permission_constants.PERMISSION_MAP["ticketing_admin"] == ["Region_Ticketing_Admin", "Region_Admin"]


def test_pattern_needs_whole_name_and_own_number(settings):
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    p = account_pattern(3)
    assert p.fullmatch("team03a") and p.fullmatch("team03i")
    assert not p.fullmatch("team30a") and not p.fullmatch("team03ab") and not p.fullmatch("blueteamadmin")
    assert not single_account()


def test_plain_pattern_names_the_single_account(settings):
    """A region whose one account per team is named otherwise: start/stop and resets act on that name."""
    settings.TEAM_ACCOUNT_PATTERN = "blue{number}"
    assert single_account()
    assert team_username(7) == "blue07"
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    assert team_username(7) == "team07"  # several accounts: Nextcloud keeps today's name


def test_team_accounts_are_matching_group_members(settings):
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    users = [user("team03b", "g3"), user("team03a", "g3"), user("blueteamadmin", "g3"), user("team03c")]
    assert team_accounts({3: "WCComps_BlueTeam03"}, users, [GROUP_3]) == {
        3: TeamAccounts(matched=["team03a", "team03b"], skipped=["blueteamadmin"])
    }  # team03c exists but isn't in the group: never a team account


def test_default_pattern_skips_blueteamadmin():
    users = [user("team03", "g3"), user("blueteamadmin", "g3")]
    assert team_accounts({3: "WCComps_BlueTeam03"}, users, [GROUP_3]) == {
        3: TeamAccounts(matched=["team03"], skipped=["blueteamadmin"])
    }


def test_team_without_a_group_has_no_accounts():
    assert team_accounts({4: "WCComps_BlueTeam04"}, [user("team04")], [GROUP_3]) == {4: TeamAccounts([], [])}


@pytest.mark.django_db
def test_readiness_fails_a_team_with_no_account_and_names_skipped(settings):
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    Team.objects.create(team_number=4, team_name="Team 4", is_active=True)
    with patch("core.admin_views.readiness.AuthentikManager") as manager:
        manager.return_value.__enter__.return_value.team_accounts_by_team.return_value = {
            4: TeamAccounts([], ["blueteamadmin"])
        }
        status, message, _ = readiness._check_team_accounts_exist()
    assert status == "fail"
    assert "04" in message and "blueteamadmin" in message


@pytest.mark.django_db
def test_password_reset_refused_with_several_accounts(admin_client, settings):
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    Team.objects.create(team_number=3, team_name="Team 3", is_active=True)
    with patch("core.admin_views.teams.reset_team_credentials") as reset:
        response = admin_client.post(reverse("admin_team_action", args=[3]), {"action": "reset_password"})
    assert response.status_code == 400
    reset.assert_not_called()
