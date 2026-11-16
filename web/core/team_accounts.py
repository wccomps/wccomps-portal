"""A team's Authentik accounts. When settings.TEAM_ACCOUNT_PATTERN names one username (team.models.single_account),
the team's one account is that username, in its group or not. Otherwise they are the members of the team's group whose
username fully matches the pattern: both conditions hold, so a loose pattern can't reach anyone outside the team
groups, and a group member with another name (an admin account in the group) is never touched.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import BadRequest

from team.models import Team, account_pattern_text, single_account, team_group_name, team_username

if TYPE_CHECKING:
    from core.authentik_manager import AuthentikGroupEntry, AuthentikManager, AuthentikUserEntry

SEVERAL_ACCOUNTS = "Teams have several accounts; manage their passwords in Authentik"


@dataclass(frozen=True)
class TeamAccounts:
    """A team group's members: those whose username matches the team's account pattern, and the rest, each sorted."""

    matched: list[str]
    skipped: list[str]


def require_single_account() -> None:
    """BadRequest for a feature that acts on a team's one account (its password, its sessions) when teams have
    several."""
    if not single_account():
        raise BadRequest(SEVERAL_ACCOUNTS)


def is_team_account(team_number: int, username: str) -> bool:
    """Whether a member of the team's group is one of its accounts when teams have several: its username fully
    matches the team's account pattern."""
    return re.fullmatch(account_pattern_text(team_number), username) is not None


def team_accounts(
    group_names: Mapping[int, str], users: list[AuthentikUserEntry], groups: list[AuthentikGroupEntry]
) -> dict[int, TeamAccounts]:
    """Each team's accounts, given its group's name; a team whose group doesn't exist has none."""
    group_pks = {group["name"]: group["pk"] for group in groups}
    result: dict[int, TeamAccounts] = {}
    for number, group_name in group_names.items():
        group_pk = group_pks.get(group_name)
        if group_pk is None:
            result[number] = TeamAccounts([], [])
            continue
        members = sorted(user["username"] for user in users if group_pk in user["groups"])
        result[number] = TeamAccounts(
            matched=[name for name in members if is_team_account(number, name)],
            skipped=[name for name in members if not is_team_account(number, name)],
        )
    return result


def team_group_names(numbers: list[int]) -> dict[int, str]:
    """Each numbered team's group: its Team.authentik_group, or team_group_name for a number with no Team row (a stop
    covers every number up to MAX_TEAMS)."""
    groups = dict(Team.objects.filter(team_number__in=numbers).values_list("team_number", "authentik_group"))
    return {n: groups.get(n) or team_group_name(n) for n in numbers}


def accounts_by_team(manager: AuthentikManager, numbers: list[int]) -> dict[int, TeamAccounts]:
    """The accounts a start or stop toggles: a single-account team's team_username, asking Authentik nothing (found by
    exact username when toggled, in its group or not); with several, its group's matching members, from one listing of
    users and one of groups."""
    if single_account():
        return {n: TeamAccounts([team_username(n)], []) for n in numbers}
    return team_accounts(team_group_names(numbers), manager.list_all_users(), manager.list_all_groups())
