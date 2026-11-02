"""The test roles: one table of each role's Authentik groups, and the one factory for a signed-in user. Fixtures
(web/conftest.py), the browser tests, the nav tests and the view property harness all make their users here."""

import itertools

from django.contrib.auth.models import User

from core.models import UserGroups
from core.permission_constants import PERMISSION_MAP, TEAM_HELD
from team.models import DiscordLink, Team, team_group_name

# Each role's Authentik groups; None is a visitor who isn't signed in
ROLE_GROUPS: dict[str, list[str] | None] = {
    "anonymous": None,
    "no_role": [],
    "blue_team": ["WCComps_BlueTeam01"],
    "red_team": ["WCComps_RedTeam"],
    "gold_team": ["WCComps_GoldTeam"],
    "white_team": ["WCComps_WhiteTeam"],
    "white_team_lead": ["WCComps_WhiteTeam", "WCComps_WhiteTeam_Lead"],
    "orange_team": ["WCComps_OrangeTeam"],
    "orange_team_lead": ["WCComps_OrangeTeam", "WCComps_OrangeTeam_Lead"],
    "ticketing_support": ["WCComps_Ticketing_Support"],
    "ticketing_admin": ["WCComps_Ticketing_Admin"],
    "admin": ["WCComps_Discord_Admin"],
}


# The teams the test worlds make active: the blue_team role's (1) and the others they set up
WORLD_TEAM_GROUPS = frozenset(team_group_name(n) for n in (1, 2, 3))


def grants(groups: list[str], permission: str) -> bool:
    """core.auth_utils.groups_grant for the test worlds, without the database: their teams 1 to 3 are active."""
    if any(group in groups for group in PERMISSION_MAP[permission]):
        return True
    return permission in TEAM_HELD and bool(WORLD_TEAM_GROUPS & set(groups))


def groups_of(role: str) -> list[str]:
    """A signed-in role's groups."""
    groups = ROLE_GROUPS[role]
    assert groups is not None, f"{role} isn't signed in"
    return groups


# The roles the role-wide browser tests (and the nav tests) visit as
BROWSER_ROLES = [
    "blue_team",
    "red_team",
    "gold_team",
    "orange_team",
    "white_team",
    "ticketing_support",
    "ticketing_admin",
    "admin",
]

# Discord ids are unique; a test's rows roll back, so a process-wide counter never collides
_discord_ids = itertools.count(7_000_000_000)


def make_user(username: str, groups: list[str]) -> User:
    """A user with these Authentik groups and an active Discord link with no team (the portal finds a team by
    Authentik group; the bot, which reads the link's, finds none)."""
    user = User.objects.create_user(username=username, password="testpass123")
    UserGroups.objects.create(user=user, authentik_id=f"{username}-uid", groups=groups)
    DiscordLink.objects.create(user=user, discord_id=next(_discord_ids), discord_username=username, is_active=True)
    return user


def role_user(role: str, *, username: str = "") -> User:
    """A user for the role (sign it in with force_login or its session). For blue_team, Team 1 (whose Authentik group
    the role holds) is made if it doesn't exist, so the user is a member of a team."""
    if role == "blue_team":
        Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01"})
    return make_user(username or f"role-{role}", groups_of(role))
