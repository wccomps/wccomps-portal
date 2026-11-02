"""Keep stored Authentik groups in step with Authentik between logins."""

import logging
from dataclasses import dataclass
from typing import NamedTuple

from django.db import connection, transaction

from core.authentik_manager import AuthentikManager
from core.models import UserGroups
from core.utils import database_clock

logger = logging.getLogger(__name__)

REFRESH_LOCK = "user_groups_refresh"


class GroupRefreshAbortedError(Exception):
    """Authentik's answer looks wrong; stored groups were left alone."""


@dataclass(frozen=True)
class GroupRefreshResult:
    checked: int
    changed: int


class _Group(NamedTuple):
    name: str
    parents: list[str]


class _User(NamedTuple):
    is_active: bool
    groups: list[str]


def _expand(direct: list[str], groups: dict[str, _Group]) -> list[str]:
    """Direct group pks plus all their ancestors, as sorted names (what the OIDC groups claim holds)."""
    seen: set[str] = set()
    stack = list(direct)
    while stack:
        pk = stack.pop()
        if pk in seen or pk not in groups:
            continue
        seen.add(pk)
        stack.extend(groups[pk].parents)
    return sorted(groups[pk].name for pk in seen)


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
    groups = {g["pk"]: _Group(g["name"], g["parents"]) for g in manager.list_all_groups()}
    users = {u["uid"]: _User(u["is_active"], u["groups"]) for u in manager.list_all_users()}
    if not users:
        raise GroupRefreshAbortedError("Authentik returned no users")
    # A short group listing would silently strip those groups from everyone (and, via the role sync,
    # their Discord roles), so any group a user or another group refers to must be in it.
    referenced = {pk for u in users.values() for pk in u.groups} | {p for g in groups.values() for p in g.parents}
    if unknown := referenced - groups.keys():
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
        current = _expand(user.groups, groups) if user else []
        active = bool(user and user.is_active)
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
