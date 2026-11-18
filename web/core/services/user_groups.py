"""Keep stored Authentik groups in step with Authentik between logins. In team folder mode a team's group comes from
the folder alone (core.team_accounts.TeamGroups.held)."""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from django.db import connection, transaction

from core.authentik_manager import AuthentikManager, AuthentikUserEntry
from core.models import UserGroups
from core.team_accounts import TeamGroups, team_folder, team_groups
from core.utils import database_clock

logger = logging.getLogger(__name__)

REFRESH_LOCK = "user_groups_refresh"


class GroupRefreshAbortedError(Exception):
    """Authentik's answer looks wrong; stored groups were left alone."""


@dataclass(frozen=True)
class GroupRefreshResult:
    checked: int
    changed: int


def with_ancestors(direct: Iterable[str], parents: Mapping[str, list[str]]) -> set[str]:
    """These group pks and all their ancestors, given each group's parent pks; a pk parents lacks is left out."""
    seen: set[str] = set()
    stack = list(direct)
    while stack:
        pk = stack.pop()
        if pk in seen or pk not in parents:
            continue
        seen.add(pk)
        stack.extend(parents[pk])
    return seen


def refresh_user_groups(manager: AuthentikManager) -> GroupRefreshResult:
    """Re-read every stored user's groups and enabled state from Authentik; a deleted user gets no groups. Waits for a
    refresh holding the lock, so no refresh commits over another's newer snapshot. GroupRefreshAbortedError or
    AuthentikAPIError, changing nothing, when Authentik's answer looks wrong."""
    with transaction.atomic():
        _take_lock(wait=True)
        return _refresh(manager)


def refresh_user_groups_unless_running(manager: AuthentikManager) -> GroupRefreshResult | None:
    """refresh_user_groups, or None at once when another refresh holds the lock."""
    with transaction.atomic():
        if not _take_lock(wait=False):
            return None
        return _refresh(manager)


def _take_lock(*, wait: bool) -> bool:
    with connection.cursor() as cursor:
        if wait:
            # Wait out a running refresh past the 30 s statement timeout, under the timer's 300 s heartbeat budget
            cursor.execute("SET LOCAL statement_timeout = '2min'")
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [REFRESH_LOCK])
            cursor.execute("SET LOCAL statement_timeout TO DEFAULT")
            return True
        cursor.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", [REFRESH_LOCK])
        return bool(cursor.fetchone()[0])


def _refresh(manager: AuthentikManager) -> GroupRefreshResult:
    read_at = database_clock()
    listed = manager.list_all_groups()
    names = {g["pk"]: g["name"] for g in listed}
    parents = {g["pk"]: g["parents"] for g in listed}
    folder = team_folder()
    users = {u["uid"]: u for u in manager.list_all_users(with_path=folder)}
    folder_teams = team_groups() if folder else None
    if not users:
        raise GroupRefreshAbortedError("Authentik returned no users")
    # A short group listing would silently strip those groups from everyone (and, via the role sync,
    # their Discord roles), so any group a user or another group refers to must be in it.
    referenced = {pk for u in users.values() for pk in u["groups"]} | {p for ps in parents.values() for p in ps}
    if unknown := referenced - parents.keys():
        raise GroupRefreshAbortedError(
            f"Authentik's group list is missing {len(unknown)} groups that users or other groups refer to"
        )

    rows = list(UserGroups.objects.select_related("user"))
    holding = [row for row in rows if row.groups]
    recognised = sum(row.authentik_id in users for row in holding)
    if holding and recognised * 2 < len(holding):
        raise GroupRefreshAbortedError(f"only {recognised} of {len(holding)} users with groups found in Authentik")

    changed = 0
    unchanged: list[int] = []
    # A sign-in that read Authentik after this refresh did keeps what it read; an older one can't land over either write
    not_newer = UserGroups.objects.exclude(groups_updated_at__gte=read_at)
    for row in rows:
        user = users.get(row.authentik_id)
        current = _current(user, names, parents, folder_teams) if user else []
        active = bool(user and user["is_active"])
        if current == sorted(row.groups) and active == row.account_active:
            unchanged.append(row.pk)
            continue
        written = not_newer.filter(pk=row.pk).update(groups=current, account_active=active, groups_updated_at=read_at)
        if not written:
            continue
        removed = sorted(set(row.groups) - set(current))
        added = sorted(set(current) - set(row.groups))
        state = "deleted in Authentik" if not user else "active" if active else "disabled"
        logger.info(f"Groups for {row.user.username} ({state}): removed {removed}, added {added}")
        changed += 1
    not_newer.filter(pk__in=unchanged).update(groups_updated_at=read_at)

    return GroupRefreshResult(checked=len(rows), changed=changed)


def _current(
    user: AuthentikUserEntry, names: dict[str, str], parents: dict[str, list[str]], folder_teams: TeamGroups | None
) -> list[str]:
    """A user's group names: its groups with their ancestors (what the OIDC groups claim holds), sorted; in team folder
    mode, team groups as the folder gives."""
    held = sorted(names[pk] for pk in with_ancestors(user["groups"], parents))
    return folder_teams.held(user, held) if folder_teams else held
