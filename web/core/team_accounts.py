"""A team's Authentik accounts. In team folder mode (settings.TEAM_ACCOUNT_PATH set, for an Authentik with no team
groups) they are the users in that folder whose username fully matches the team's pattern. Otherwise, when
settings.TEAM_ACCOUNT_PATTERN names one username (team.models.single_account), the team's one account is that username,
in its group or not; with several they are the members of the team's group whose username fully matches the pattern.
Both conditions hold either way, so a loose pattern can't reach anyone outside the folder or the team groups, and a
folder or group member with another name (an admin account in the group) is never touched.
"""

import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import BadRequest

from team.models import MAX_TEAMS, Team, account_pattern_text, single_account, team_group_name, team_username

if TYPE_CHECKING:
    from core.authentik_manager import AuthentikGroupEntry, AuthentikManager, AuthentikUserEntry

SEVERAL_ACCOUNTS = "Teams have several accounts; manage their passwords in Authentik"


@dataclass(frozen=True)
class TeamAccounts:
    """A team's accounts (matched) and, with team groups, its group's other members (skipped), each sorted."""

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


def team_folder() -> bool:
    """Whether teams come from the team folder (settings.TEAM_ACCOUNT_PATH) rather than from Authentik groups."""
    return bool(settings.TEAM_ACCOUNT_PATH)


def folder_team_number(user: Mapping[str, object]) -> int | None:
    """The team a user listed with its path is on in team folder mode: in the team folder, with a username fully
    matching exactly one team's account pattern. None otherwise, team folder mode off included."""
    username = user.get("username")
    if not team_folder() or user.get("path") != settings.TEAM_ACCOUNT_PATH or not isinstance(username, str):
        return None
    numbers = [n for n in range(1, MAX_TEAMS + 1) if is_team_account(n, username)]
    return numbers[0] if len(numbers) == 1 else None


def blueteam_kinds() -> tuple[str, ...]:
    """What a controlled application's blue team binding may bind, the preferred first: in team folder mode a policy
    (the folder has no group to bind) or a group, otherwise a group only."""
    return ("policy", "group") if team_folder() else ("group",)


@dataclass(frozen=True)
class TeamGroups:
    """In team folder mode, each team's group by number (team_group_names over every number) and every name that is a
    team's group (each Team.authentik_group and each TEAM_GROUP_PATTERN name), which only the folder grants."""

    by_number: dict[int, str]
    names: frozenset[str]

    def held(self, account: Mapping[str, object] | None, groups: Iterable[str]) -> list[str]:
        """The groups a user holds in team folder mode: its groups (from Authentik or the claim) other than team groups,
        plus its team's group when the account (as listed with its path) is in the folder and folder_team_number gives
        it a team. Neither a claim nor an Authentik group makes anyone a team's."""
        held = {group for group in groups if group not in self.names}
        number = folder_team_number(account) if account is not None else None
        if number is not None:
            held.add(self.by_number[number])
        return sorted(held)


def team_groups() -> TeamGroups:
    """TeamGroups, read from the database."""
    numbers = list(range(1, MAX_TEAMS + 1))
    by_number = team_group_names(numbers)
    return TeamGroups(by_number, frozenset({*by_number.values(), *map(team_group_name, numbers)}))


FOLDER_CACHE_SECONDS = 60.0


class _FolderCache:
    """This process's recent reads of the team folder: each account seen, and each uid a full listing lacked, with
    when. One lookup reads Authentik at a time, so a burst of sign-ins shares its reads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.accounts: dict[str, tuple[AuthentikUserEntry, float]] = {}
        self.absent: dict[str, float] = {}

    def answer(self, uid: str, now: float) -> tuple[bool, AuthentikUserEntry | None]:
        """(known, account): known when a read in the last FOLDER_CACHE_SECONDS found the uid in the folder, or
        listed the whole folder without it."""
        cached = self.accounts.get(uid)
        if cached and now - cached[1] < FOLDER_CACHE_SECONDS:
            return True, cached[0]
        lacked_at = self.absent.get(uid)
        return lacked_at is not None and now - lacked_at < FOLDER_CACHE_SECONDS, None

    def forget_stale(self, now: float) -> None:
        self.accounts = {u: e for u, e in self.accounts.items() if now - e[1] < FOLDER_CACHE_SECONDS}
        self.absent = {u: t for u, t in self.absent.items() if now - t < FOLDER_CACHE_SECONDS}


_folder_cache = _FolderCache()


def team_folder_users(manager: AuthentikManager) -> list[AuthentikUserEntry]:
    """Every user in the team folder (settings.TEAM_ACCOUNT_PATH), from one listing."""
    return [user for page in manager.team_folder_pages(settings.TEAM_ACCOUNT_PATH) for user in page]


def team_folder_account(manager: AuthentikManager, uid: str) -> AuthentikUserEntry | None:
    """The team folder account with this uid, or None; cached per process for FOLDER_CACHE_SECONDS, a uid the folder
    lacks included. AuthentikAPIError when Authentik can't be asked."""
    # Held through the read, so one read serves a burst of sign-ins; the HTTPX timeouts bound the wait
    with _folder_cache.lock:
        known, account = _folder_cache.answer(uid, time.monotonic())
        if known:
            return account
        _folder_cache.forget_stale(time.monotonic())
        for page in manager.team_folder_pages(settings.TEAM_ACCOUNT_PATH):
            read_at = time.monotonic()
            for user in page:
                _folder_cache.accounts[user["uid"]] = (user, read_at)
            if found := next((user for user in page if user["uid"] == uid), None):
                return found
        _folder_cache.absent[uid] = time.monotonic()
        return None


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
    """The accounts a start or stop toggles: in team folder mode, the folder's users matching the team's pattern, from
    one listing of the folder; otherwise a single-account team's team_username, asking Authentik nothing (found by
    exact username when toggled, in its group or not); with several, its group's matching members, from one listing of
    users and one of groups."""
    if team_folder():
        folder = team_folder_users(manager)
        return {
            n: TeamAccounts(sorted(u["username"] for u in folder if folder_team_number(u) == n), []) for n in numbers
        }
    if single_account():
        return {n: TeamAccounts([team_username(n)], []) for n in numbers}
    return team_accounts(team_group_names(numbers), manager.list_all_users(), manager.list_all_groups())
