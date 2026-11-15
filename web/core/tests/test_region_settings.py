"""Authentik group names, team accounts, the team cap, hosts, names and the box-reset category come from settings.
Their defaults are neutral; WRCCDC's values live in its production configmap (WRCCDC_ENV), which the suite runs as."""

import importlib
import runpy
from pathlib import Path
from unittest.mock import patch

import portal
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from portal.required import REQUIRED_SETTINGS, missing_settings

from core import permission_constants
from core.admin_views import readiness
from core.authentik_manager import AuthentikGroupEntry, AuthentikUserEntry
from core.team_accounts import TeamAccounts, team_accounts
from team.models import MAX_TEAMS, Team, single_account, team_group_name, team_username

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
# WRCCDC's production configmap (and portal.test_settings)
WRCCDC_ENV = {
    "PORTAL_GROUP_PREFIX": "WCComps_",
    "PORTAL_ADMIN_GROUP": "WCComps_Discord_Admin",
    "ORG_NAME": "Western Cyber Competitions",
    "ORG_SHORT_NAME": "WCComps",
    "AUTHENTIK_URL": "https://auth.wccomps.org",
    "QUOTIENT_API_URL": "https://scoring.wccomps.org",
    "DEFAULT_FROM_EMAIL": "noreply@wccomps.org",
    "DEFAULT_REPLY_TO_EMAIL": "info@wccomps.org",
}
# Every variable the neutral defaults stand in for
REGION_ENV = (
    *WRCCDC_ENV,
    "AUTHENTIK_OIDC_URL",
    "SERVER_EMAIL",
    "SITE_NAME",
    "TEAM_ACCOUNT_PATTERN",
    "TEAM_GROUP_PATTERN",
    "MAX_TEAMS",
    "BOX_RESET_CATEGORY_ID",
    "ALLOWED_HOSTS",
    "BASE_URL",
)
SETTINGS_PATH = str(Path(portal.__file__).parent / "settings.py")


def load_settings(monkeypatch, env: dict[str, str]) -> dict[str, object]:
    """settings.py run with only env among the region variables."""
    for name in REGION_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return runpy.run_path(SETTINGS_PATH)


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


def test_suite_runs_as_wrccdc():
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
    names = ["team03a", "team03i", "team30a", "team03ab", "blueteamadmin"]
    accounts = team_accounts({3: "WCComps_BlueTeam03"}, [user(name, "g3") for name in names], [GROUP_3])
    assert accounts[3].matched == ["team03a", "team03i"]
    assert not single_account()


def test_plain_pattern_names_the_single_account(settings):
    """A region whose one account per team is named otherwise: start/stop and resets act on that name."""
    settings.TEAM_ACCOUNT_PATTERN = "blue{number}"
    assert single_account()
    assert team_username(7) == "blue07"
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    assert team_username(7) == "team07"  # several accounts: Nextcloud keeps today's name


@pytest.mark.parametrize("pattern", ["blue-{number}", "team {number}", "team#{number}", "a&b~{number}", "t.{number}"])
def test_pattern_without_regex_operators_is_one_account(settings, pattern):
    settings.TEAM_ACCOUNT_PATTERN = pattern
    assert single_account()
    assert team_username(7) == pattern.replace("{number}", "07")


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
    Team.objects.create(team_number=4, team_name="Team 4", is_active=True, authentik_group="WCComps_BlueTeam04")
    with patch("core.admin_views.readiness.AuthentikManager") as manager:
        mgr = manager.return_value.__enter__.return_value
        mgr.list_all_users.return_value = [user("blueteamadmin", "g4")]
        mgr.list_all_groups.return_value = [{"pk": "g4", "name": "WCComps_BlueTeam04", "parents": []}]
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


@pytest.mark.parametrize(
    ("base_url", "origins"),
    [
        ("https://portal.example.org", ["https://portal.example.org"]),
        ("https://portal.example.org:8443/path", ["https://portal.example.org:8443"]),
        ("http://localhost:5000", []),
    ],
)
def test_csrf_trusted_origins_is_the_https_base_url_origin(monkeypatch, base_url, origins):
    """An http BASE_URL (local development) trusts nothing: production serves https only."""
    assert load_settings(monkeypatch, {"BASE_URL": base_url})["CSRF_TRUSTED_ORIGINS"] == origins


def test_neutral_defaults(monkeypatch):
    prod = load_settings(monkeypatch, {})

    assert prod["CSRF_TRUSTED_ORIGINS"] == []
    assert prod["ALLOWED_HOSTS"] == ["localhost", "127.0.0.1"]
    assert prod["AUTHENTIK_URL"] == ""
    assert prod["AUTHENTIK_OIDC_URL"] == ""
    assert prod["QUOTIENT_API_URL"] == ""
    assert prod["DEFAULT_FROM_EMAIL"] == ""
    assert prod["SERVER_EMAIL"] == "root@localhost"
    assert prod["DEFAULT_REPLY_TO_EMAIL"] == ""
    assert prod["PORTAL_GROUP_PREFIX"] == "Portal_"
    assert prod["PORTAL_ADMIN_GROUP"] == "Portal_Admin"
    assert prod["TEAM_GROUP_PATTERN"] == "Portal_BlueTeam{number:02d}"
    assert prod["TEAM_ACCOUNT_PATTERN"] == "team{number}"
    assert prod["ORG_NAME"] == ""
    assert prod["ORG_SHORT_NAME"] == ""
    assert prod["SITE_NAME"] == "Portal"
    assert prod["MAX_TEAMS"] == 50
    assert prod["BOX_RESET_CATEGORY_ID"] == 2


def test_short_name_names_the_site(monkeypatch):
    prod = load_settings(monkeypatch, {"ORG_SHORT_NAME": "Example", "DEFAULT_FROM_EMAIL": "portal@example.org"})

    assert prod["SITE_NAME"] == "Example Portal"
    assert prod["SERVER_EMAIL"] == "portal@example.org"


def test_wrccdc_configmap_gives_today(monkeypatch, settings, reload_permissions):
    """WRCCDC's configmap and production BASE_URL give today's origin, groups and names."""
    prod = load_settings(monkeypatch, WRCCDC_ENV | {"BASE_URL": "https://portal.wccomps.org"})

    assert prod["CSRF_TRUSTED_ORIGINS"] == ["https://portal.wccomps.org"]
    assert prod["SITE_NAME"] == "WCComps Portal"
    assert prod["AUTHENTIK_OIDC_URL"] == "https://auth.wccomps.org/application/o/discord-bot/"
    assert prod["SERVER_EMAIL"] == "noreply@wccomps.org"
    assert prod["TEAM_GROUP_PATTERN"] == "WCComps_BlueTeam{number:02d}"
    settings.PORTAL_GROUP_PREFIX, settings.PORTAL_ADMIN_GROUP = prod["PORTAL_GROUP_PREFIX"], prod["PORTAL_ADMIN_GROUP"]
    reload_permissions()
    assert permission_constants.PERMISSION_MAP == TODAY_PERMISSION_MAP


@pytest.mark.parametrize("name", REQUIRED_SETTINGS)
def test_missing_settings_names_each_empty_required_one(settings, name):
    assert missing_settings() == []
    setattr(settings, name, "")
    assert missing_settings() == [name]


def test_missing_settings_names_every_empty_one(settings):
    for name in REQUIRED_SETTINGS:
        setattr(settings, name, "")
    assert missing_settings() == list(REQUIRED_SETTINGS)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TEAM_ACCOUNT_PATTERN", "blueteam"),
        ("TEAM_ACCOUNT_PATTERN", "team{number:02d}"),  # only the exact {number} is replaced
        ("TEAM_GROUP_PATTERN", "BlueTeam"),
        ("TEAM_GROUP_PATTERN", "BlueTeam{numbers}"),
    ],
)
def test_pattern_without_number_is_refused_at_load(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured, match=name):
        runpy.run_path(SETTINGS_PATH)


def test_site_name_reaches_pages(admin_client, settings):
    settings.SITE_NAME = "Example Portal"
    page = admin_client.get(reverse("admin_teams")).content.decode()
    assert "Example Portal" in page
    assert "WCComps Portal" not in page


@pytest.mark.django_db
def test_services_refuse_resets_with_several_accounts(settings):
    """The services answer Refused, which the bot shows, and ask Authentik nothing."""
    from core.authentik_utils import Refused, reset_team, reset_team_credentials, reset_teams_credentials
    from core.team_accounts import SEVERAL_ACCOUNTS
    from core.utils import drain

    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    with patch("core.authentik_utils.AuthentikManager") as manager:
        assert reset_team_credentials(3, actor="admin") == Refused(SEVERAL_ACCOUNTS)
        assert reset_team(3, actor="admin", recreate_channels=True).credentials == Refused(SEVERAL_ACCOUNTS)
        bulk = drain(reset_teams_credentials([3, 4], "admin"))
    manager.assert_not_called()
    assert bulk.failed_resets == [("team03", SEVERAL_ACCOUNTS), ("team04", SEVERAL_ACCOUNTS)]
