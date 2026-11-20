"""Authentik group names, team accounts, the team cap, hosts, names and the box-reset category come from settings.
Their defaults are neutral; WRCCDC's values live in its production configmap (WRCCDC_ENV), which the suite runs as."""

import importlib
import runpy
from pathlib import Path
from unittest.mock import patch

import portal
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from portal.required import REQUIRED_SETTINGS, missing_settings

from bot.role_sync import role_grants
from core import auth_utils, permission_constants
from core.admin_views import readiness
from core.authentik_manager import AuthentikGroupEntry, AuthentikUserEntry
from core.models import AuditLog, QueuedTask, UserGroups
from core.team_accounts import SEVERAL_ACCOUNTS, TeamAccounts, team_accounts
from team.models import MAX_TEAMS, DiscordLink, Team, single_account, team_group_name, team_username

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
# The settings naming each role's Authentik group
ROLE_GROUP_SETTINGS = (
    "PORTAL_ADMIN_GROUP",
    "PORTAL_GOLD_GROUP",
    "PORTAL_WHITE_GROUP",
    "PORTAL_WHITE_LEAD_GROUP",
    "PORTAL_ORANGE_GROUP",
    "PORTAL_ORANGE_LEAD_GROUP",
    "PORTAL_RED_GROUP",
    "PORTAL_BLACK_GROUP",
    "PORTAL_TICKETING_ADMIN_GROUP",
    "PORTAL_TICKETING_SUPPORT_GROUP",
)
# Every variable the neutral defaults stand in for
REGION_ENV = (
    *WRCCDC_ENV,
    *ROLE_GROUP_SETTINGS,
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
def reload_permissions(settings, monkeypatch):
    """Reload permission_constants under the test's settings (auth_utils, which decides grants, sees the new map), then
    put back the map built from the defaults. This teardown runs before the settings fixture restores them, so
    reloading here would keep the test's names."""
    original = permission_constants.PERMISSION_MAP

    def reload() -> None:
        importlib.reload(permission_constants)
        monkeypatch.setattr(auth_utils, "PERMISSION_MAP", permission_constants.PERMISSION_MAP)

    yield reload
    permission_constants.PERMISSION_MAP = original


def use_groups(settings, prod: dict[str, object]) -> None:
    """The role group settings settings.py gave prod, as the running settings."""
    for name in ROLE_GROUP_SETTINGS:
        setattr(settings, name, prod[name])
    settings.PORTAL_ROLE_GROUPS = prod["PORTAL_ROLE_GROUPS"]


def test_suite_runs_as_wrccdc():
    assert permission_constants.PERMISSION_MAP == TODAY_PERMISSION_MAP
    assert team_username(7) == "team07"
    assert team_group_name(7) == "WCComps_BlueTeam07"
    assert MAX_TEAMS == 50
    assert single_account()


def test_prefix_changes_every_group(monkeypatch, settings, reload_permissions):
    prod = load_settings(monkeypatch, {"PORTAL_GROUP_PREFIX": "Region_"})
    use_groups(settings, prod)
    reload_permissions()
    assert permission_constants.PERMISSION_MAP["ticketing_admin"] == ["Region_Ticketing_Admin", "Region_Admin"]
    groups = {group for groups in permission_constants.PERMISSION_MAP.values() for group in groups}
    assert all(group.startswith("Region_") for group in [*groups, *prod["GROUP_ROLE_MAPPING"]])


@pytest.mark.parametrize("name", ["Region_Support", "Green Team"])
def test_a_role_group_named_directly_grants_the_role(monkeypatch, settings, reload_permissions, name):
    """A region names a role's group outright (here ticketing support, as Green team), spaces and all: that group
    grants the role and the prefixed default no longer does; the other roles keep their default names."""
    prod = load_settings(monkeypatch, WRCCDC_ENV | {"PORTAL_TICKETING_SUPPORT_GROUP": name})
    use_groups(settings, prod)
    reload_permissions()

    assert auth_utils.groups_grant([name], "ticketing_support")
    assert not auth_utils.groups_grant(["WCComps_Ticketing_Support"], "ticketing_support")
    assert not auth_utils.groups_grant([name], "ticketing_admin")
    assert auth_utils.groups_grant(["WCComps_Ticketing_Admin"], "ticketing_support")
    assert permission_constants.PERMISSION_MAP["gold_team"] == ["WCComps_GoldTeam", "WCComps_Discord_Admin"]


@pytest.mark.django_db
def test_a_spaced_group_name_reaches_permissions_and_discord_roles(monkeypatch, settings, reload_permissions):
    """A signed-in member of "White Team" holds the white team permission, and role sync gives a member of "White
    Team" and "Black Team" both groups' Discord roles."""
    env = {
        "PORTAL_WHITE_GROUP": "White Team",
        "PORTAL_BLACK_GROUP": "Black Team",
        "WHITETEAM_ROLE_ID": "8",
        "BLACKTEAM_ROLE_ID": "7",
    }
    prod = load_settings(monkeypatch, WRCCDC_ENV | env)
    use_groups(settings, prod)
    settings.GROUP_ROLE_MAPPING = prod["GROUP_ROLE_MAPPING"]
    reload_permissions()
    member = User.objects.create_user("judge")
    UserGroups.objects.create(user=member, authentik_id="judge", groups=["White Team", "Black Team"])
    DiscordLink.objects.create(user=member, discord_id=77, discord_username="judge", is_active=True)

    assert auth_utils.has_permission(member, "white_team")
    assert list(auth_utils.users_granted("white_team")) == [member]
    assert not auth_utils.has_permission(member, "gold_team")
    grants = role_grants()
    assert grants.by_member == {77: frozenset({7, 8})}
    assert {7, 8} <= grants.managed


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
@pytest.mark.parametrize(
    ("action", "message"),
    [("reset_password", f"Password reset failed: {SEVERAL_ACCOUNTS}"), ("reset", SEVERAL_ACCOUNTS)],
)
def test_password_reset_refused_with_several_accounts(admin_client, settings, action, message):
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    Team.objects.create(team_number=3, team_name="Team 3", is_active=True)
    with patch("core.authentik_utils.AuthentikManager") as manager:
        response = admin_client.post(reverse("admin_team_action", args=[3]), {"action": action})
    assert response.json() == {"success": False, "message": message}
    manager.assert_not_called()
    assert not AuditLog.objects.exists()
    assert not QueuedTask.objects.exists()


@pytest.mark.django_db
def test_group_fix_refused_with_several_accounts(admin_client, settings):
    """Fix Group Membership adds each team's one account to its group: with several accounts it asks Authentik
    nothing and says why."""
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    Team.objects.create(team_number=3, team_name="Team 3", is_active=True)
    with patch("core.admin_views.readiness.AuthentikManager") as manager:
        response = admin_client.post(
            reverse("admin_competition_action"), {"action": "fix_group_membership", "readiness_fix": "1"}
        )
    assert response.status_code == 400
    assert response.json() == {"error": SEVERAL_ACCOUNTS}
    manager.assert_not_called()


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
    assert prod["PORTAL_GOLD_GROUP"] == "Portal_GoldTeam"
    assert prod["PORTAL_WHITE_LEAD_GROUP"] == "Portal_WhiteTeam_Lead"
    assert prod["PORTAL_BLACK_GROUP"] == "Portal_BlackTeam"
    assert prod["PORTAL_TICKETING_SUPPORT_GROUP"] == "Portal_Ticketing_Support"
    assert prod["TEAM_GROUP_PATTERN"] == "Portal_BlueTeam{number:02d}"
    assert prod["TEAM_ACCOUNT_PATTERN"] == "team{number}"
    assert prod["ORG_NAME"] == ""
    assert prod["ORG_SHORT_NAME"] == ""
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
    assert set(prod["GROUP_ROLE_MAPPING"]) == {  # settings.py at f207dc23
        "WCComps_BlackTeam",
        "WCComps_WhiteTeam",
        "WCComps_OrangeTeam",
        "WCComps_RedTeam",
        "WCComps_GoldTeam",
    }
    use_groups(settings, prod)
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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ZULIP_TEAM_BROADCAST_STREAM", "automation"),  # one stream every team sees
        ("ZULIP_TEAM_BROADCAST_STREAM", "Team {num} Chat"),
        ("ZULIP_TEAM_BROADCAST_STREAM", "Team {number[0]} Chat"),
        ("ZULIP_TEAM_BROADCAST_TOPIC", "news {num}"),
        ("ZULIP_TEAM_BROADCAST_TOPIC", "news {"),
        ("ZULIP_TEAM_BROADCAST_TOPIC", "news {0}"),
    ],
)
def test_bad_broadcast_stream_or_topic_is_refused_at_load(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ImproperlyConfigured, match=name):
        runpy.run_path(SETTINGS_PATH)


def test_a_broadcast_stream_teams_past_two_share_is_refused_at_load(monkeypatch):
    """Teams 1 and 2 differ, but teams 1 and 10 to 19 all get "Team 1 Chat"."""
    monkeypatch.setenv("ZULIP_TEAM_BROADCAST_STREAM", "Team {number!s:.1} Chat")
    with pytest.raises(ImproperlyConfigured, match="every team from 1 to MAX_TEAMS its own stream"):
        runpy.run_path(SETTINGS_PATH)


def test_a_teams_own_ticket_stream_may_be_its_broadcast_stream(monkeypatch):
    monkeypatch.setenv("ZULIP_TEAM_BROADCAST_STREAM", "team{number:02d}")
    assert runpy.run_path(SETTINGS_PATH)["ZULIP_TEAM_BROADCAST_STREAM"] == "team{number:02d}"


def test_a_broadcast_stream_naming_another_teams_ticket_stream_is_refused_at_load(monkeypatch):
    """Team 1's broadcasts in "Team 10 Support", team 10's ticket stream, would show to team 10."""
    monkeypatch.setenv("ZULIP_TEAM_STREAM", "Team {number} Support")
    monkeypatch.setenv("ZULIP_TEAM_BROADCAST_STREAM", "Team {number}0 Support")
    with pytest.raises(ImproperlyConfigured, match="team 01 the stream 'Team 10 Support', team 10's ZULIP_TEAM_STREAM"):
        runpy.run_path(SETTINGS_PATH)


def test_an_empty_broadcast_topic_keeps_its_default(monkeypatch):
    monkeypatch.setenv("ZULIP_TEAM_BROADCAST_TOPIC", "")
    assert runpy.run_path(SETTINGS_PATH)["ZULIP_TEAM_BROADCAST_TOPIC"] == "announcements"


def test_broadcast_stream_per_team_and_plain_topic_load(monkeypatch):
    monkeypatch.setenv("ZULIP_TEAM_BROADCAST_STREAM", "Team {number:02d} Chat")
    monkeypatch.setenv("ZULIP_TEAM_BROADCAST_TOPIC", "automation-messages")
    prod = runpy.run_path(SETTINGS_PATH)
    assert prod["ZULIP_TEAM_BROADCAST_STREAM"].format(number=3) == "Team 03 Chat"


@pytest.mark.parametrize(
    ("first", "second"),
    [("PORTAL_WHITE_GROUP", "PORTAL_ORANGE_GROUP"), ("PORTAL_ADMIN_GROUP", "PORTAL_TICKETING_SUPPORT_GROUP")],
)
def test_two_roles_naming_one_group_are_refused_at_load(monkeypatch, first, second):
    env = {first: "Volunteers", second: "Volunteers"}
    with pytest.raises(ImproperlyConfigured, match=f"{first} and {second} both name"):
        load_settings(monkeypatch, env)


def test_a_role_group_set_empty_keeps_its_default(monkeypatch):
    prod = load_settings(
        monkeypatch, {"PORTAL_ADMIN_GROUP": "", "PORTAL_RED_GROUP": "", "PORTAL_TICKETING_SUPPORT_GROUP": ""}
    )
    assert prod["PORTAL_ADMIN_GROUP"] == "Portal_Admin"
    assert prod["PORTAL_RED_GROUP"] == "Portal_RedTeam"
    assert prod["PORTAL_TICKETING_SUPPORT_GROUP"] == "Portal_Ticketing_Support"


def test_every_role_group_setting_is_in_the_role_table(monkeypatch):
    """PERMISSION_MAP, GROUP_ROLE_MAPPING and the duplicate check all read PORTAL_ROLE_GROUPS."""
    prod = load_settings(monkeypatch, {})
    roles = prod["PORTAL_ROLE_GROUPS"]
    assert isinstance(roles, dict)
    assert sorted(f"PORTAL_{role.upper()}_GROUP" for role in roles) == sorted(ROLE_GROUP_SETTINGS)
    assert all(prod[f"PORTAL_{role.upper()}_GROUP"] == group for role, group in roles.items())


def test_site_name_reaches_pages(admin_client, settings):
    settings.SITE_NAME = "Example Portal"
    page = admin_client.get(reverse("admin_teams")).content.decode()
    assert "Example Portal" in page
    assert "WCComps Portal" not in page


@pytest.mark.django_db
def test_services_refuse_resets_with_several_accounts(settings):
    """The services answer Refused, which the bot shows, and ask Authentik nothing."""
    from core.authentik_utils import (
        Refused,
        TeamReset,
        reset_team,
        reset_team_credentials,
        reset_teams_credentials,
        set_team_password,
    )
    from core.utils import drain

    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    with patch("core.authentik_utils.AuthentikManager") as manager:
        assert reset_team_credentials(3, actor="admin") == Refused(SEVERAL_ACCOUNTS)
        refused = TeamReset(Refused(SEVERAL_ACCOUNTS), recreation_queued=False)
        assert reset_team(3, actor="admin", recreate_channels=True) == refused  # No recreation promised
        bulk = drain(reset_teams_credentials([3, 4], "admin"))
        assert list(reset_teams_credentials([], "admin")) == []  # No empty batch for the bot to name
        assert set_team_password(3, actor="packets") == (None, SEVERAL_ACCOUNTS)
    manager.assert_not_called()
    assert bulk.failed_resets == [("team03", SEVERAL_ACCOUNTS), ("team04", SEVERAL_ACCOUNTS)]
