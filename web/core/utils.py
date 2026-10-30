import functools
import ipaddress
import json
import logging
import queue
import re
import threading
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Page, Paginator
from django.db import DatabaseError, InterfaceError, connection, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Model, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

from core.discord_tasks import SyncRolesResult

if TYPE_CHECKING:
    from team.models import Team

logger = logging.getLogger(__name__)


type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def client_ip(request: HttpRequest) -> IPAddress:
    """The client's address: CF-Connecting-IP when TRUST_CF_CONNECTING_IP is on and it parses, else REMOTE_ADDR, else
    0.0.0.0."""
    header = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    if settings.TRUST_CF_CONNECTING_IP:
        if (address := _parse_address(header)) is not None:
            return address
    elif header:
        _warn_untrusted_cf_header()
    peer = _parse_address(request.META.get("REMOTE_ADDR", "").partition("%")[0])
    return ipaddress.IPv4Address(0) if peer is None else peer


def _parse_address(text: str) -> IPAddress | None:
    """text as an address, an IPv4-mapped one as its IPv4; None for anything else, or an IPv6 scope ID (free text)."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            return None
        return address.ipv4_mapped or address
    return address


@functools.cache
def _warn_untrusted_cf_header() -> None:
    logger.warning(
        "A request carried CF-Connecting-IP, which is ignored because TRUST_CF_CONNECTING_IP is off: behind "
        "Cloudflare, set it to true, or every client shares the proxy's address in rate limits and logs"
    )


class UnknownTimezoneError(ValueError):
    """A schedule time named a timezone that zoneinfo doesn't know."""


def parse_datetime_to_utc(datetime_str: str, tz_name: str = "America/Los_Angeles") -> datetime:
    """Parse a YYYY-MM-DDTHH:MM string in timezone tz_name and convert to UTC; raises ValueError."""
    dt = datetime.strptime(datetime_str, "%Y-%m-%dT%H:%M")
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError, ValueError:
        raise UnknownTimezoneError(f"Unknown timezone: {tz_name}") from None
    local_time = datetime(dt.year, dt.month, dt.day, dt.hour, dt.minute, tzinfo=zone)
    try:
        return local_time.astimezone(ZoneInfo("UTC"))
    except OverflowError as e:  # year 1 or 9999, pushed past the calendar by the zone's offset
        raise ValueError(f"{datetime_str} in {tz_name} is outside the calendar") from e


def shorten(text: str, limit: int) -> str:
    """A preview of the text: its first `limit` characters, back to the last whitespace in their second half (inside a
    word when there is none), then "..." and a ")" for each parenthesis the cut left open. truncatechars and
    truncatewords can leave one open."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if not text[limit].isspace():
        last_break = max((m.start() for m in re.finditer(r"\s", cut)), default=-1)
        if last_break >= limit // 2:
            cut = cut[:last_break]
    cut = re.sub(r"[\s,;:.(]+$", "", cut)
    return cut + "..." + ")" * open_parentheses(cut)


def open_parentheses(text: str) -> int:
    """How many of the text's parentheses are still open at its end."""
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
    return depth


def ndjson_progress(step: str, current: int, total: int, ok: bool = True) -> str:
    """Encode a single progress line as newline-delimited JSON for streaming views."""
    return json.dumps({"step": step, "current": current, "total": total, "ok": ok}) + "\n"


def ndjson_done(message: str, success: bool = True) -> str:
    """Encode the final line of a streamed operation, which the page shows as the outcome."""
    return json.dumps({"done": True, "success": success, "message": message}) + "\n"


@contextmanager
def advisory_lock(key: int | str, *, wait: bool = False, conn: BaseDatabaseWrapper | None = None) -> Iterator[bool]:
    """Yields whether the lock was taken; re-entrant within one connection."""
    db = conn or connection
    lock_id = "%s" if isinstance(key, int) else "hashtextextended(%s, 0)"
    with db.cursor() as cursor:
        if wait:
            cursor.execute(f"SELECT pg_advisory_lock({lock_id})", [key])  # noqa: S608 - fixed fragments
            acquired = True
        else:
            cursor.execute(f"SELECT pg_try_advisory_lock({lock_id})", [key])  # noqa: S608 - fixed fragments
            row = cursor.fetchone()
            acquired = bool(row and row[0])
    try:
        yield acquired
    finally:
        if acquired:
            try:
                with db.cursor() as cursor:
                    cursor.execute(f"SELECT pg_advisory_unlock({lock_id})", [key])  # noqa: S608 - fixed fragments
            except DatabaseError, InterfaceError:
                # Only a closed connection has released it; an aborted transaction still holds it
                if db.connection is not None and not db.connection.closed:
                    raise


def _inline(lines: Iterator[str]) -> Iterator[str]:
    """The operation's lines, ending with a failure line if it raises."""
    try:
        yield from lines
    except Exception as e:
        logger.exception("Streamed operation failed")
        yield ndjson_done(f"Failed: {e}", success=False)


RUN_DETACHED_THREAD = "run-detached"


def run_detached(lines: Iterator[str]) -> Iterator[str]:
    """Run a streamed operation to the end in its own thread, relaying its lines while the client listens.

    A streaming view's generator stops at its next line once the client goes away (a closed tab, a proxy
    timeout), leaving the operation half done. Here the client only stops the relay; the work finishes.
    """
    relay: queue.Queue[str | None] = queue.Queue()

    def run() -> None:
        try:
            for line in _inline(lines):
                relay.put(line)
        finally:
            relay.put(None)

    _start(run)
    while (line := relay.get()) is not None:
        yield line


def start_detached(lines: Iterator[str]) -> None:
    """Run a streamed operation to the end in its own thread, with nobody reading its lines.

    The thread starts once the caller's transaction commits (at once outside one), so it sees what the caller wrote.
    """

    def run() -> None:
        for _line in _inline(lines):
            pass

    transaction.on_commit(lambda: _start(run))


def _start(run: Callable[[], None]) -> None:
    """Run it in a thread of its own, which closes its DB connection when done: nothing else closes that thread's."""

    def target() -> None:
        try:
            run()
        finally:
            connection.close()

    # Not a daemon: a worker shutting down (a deploy) waits for the operation, up to gunicorn's graceful timeout
    threading.Thread(target=target, name=RUN_DETACHED_THREAD, daemon=False).start()


def optional_int(value: str) -> int | None:
    """The integer in request text, or None when the text isn't one."""
    try:
        return int(value)
    except ValueError:
        return None


class FilterSortPage[M: Model](TypedDict):
    page_obj: Page[M]
    current_sort: str


def list_filters(request: HttpRequest, defaults: Mapping[str, str]) -> tuple[dict[str, str], bool]:
    """A list page's filters from the request, each its default when absent or blank (values trimmed), and whether
    any is set to something else: the page's empty state then says its filters hide rows."""
    values = {name: request.GET.get(name, "").strip() or default for name, default in defaults.items()}
    return values, any(values[name] != default for name, default in defaults.items())


def by_approval[M: Model](queryset: QuerySet[M], status: str, field: str = "is_approved") -> QuerySet[M]:
    """The rows the approval-status filter (cotton/approval_filter.html) shows: pending (not approved yet), approved,
    or all (any other value)."""
    if status in ("pending", "approved"):
        return queryset.filter(**{field: status == "approved"})
    return queryset


def chosen_team(team: str) -> int | None:
    """The team number the team filter (?team=) names (07 and +7 name team 7), or None when it names no team."""
    from team.models import Team

    number = optional_int(team)
    return number if number is not None and Team.objects.filter(team_number=number).exists() else None


def team_numbers(teams: QuerySet[Team], team: int | None) -> list[int]:
    """The team numbers a team filter offers: the teams with rows, and the one chosen (chosen_team) even with none."""
    numbers = set(teams.values_list("team_number", flat=True))
    if team is not None:
        numbers.add(team)
    return sorted(numbers)


def by_team[M: Model](queryset: QuerySet[M], team: int | None, field: str = "team") -> QuerySet[M]:
    """The rows of the chosen team (chosen_team), or every row when none is chosen."""
    if team is None:
        return queryset
    return queryset.filter(**{f"{field}__team_number": team})


def requested_sort(request: HttpRequest, sort_fields: Collection[str], default_sort: str) -> str:
    """The list's ?sort=: one of sort_fields, with a leading - for descending; "default" means the model's default
    ordering (an empty sort), and any other value the list's default sort."""
    sort_by = request.GET.get("sort", default_sort)
    if sort_by == "default":
        return ""
    return sort_by if sort_by.removeprefix("-") in sort_fields else default_sort


def stable_order[M: Model](queryset: QuerySet[M], sort_by: str) -> QuerySet[M]:
    """The queryset ordered by sort_by, then its model's default ordering and pk, so rows that tie keep one order
    and a page boundary never splits them differently from one request to the next."""
    default_ordering = list(queryset.model._meta.ordering or ())
    return queryset.order_by(*([sort_by] if sort_by else []), *default_ordering, "pk")


def filter_sort_paginate[M: Model](
    request: HttpRequest,
    queryset: QuerySet[M],
    *,
    sort_fields: Collection[str],
    default_sort: str = "-created_at",
    page_size: int = 50,
) -> FilterSortPage[M]:
    """Order a filtered queryset by requested_sort and paginate it by ?page=."""
    sort_by = requested_sort(request, sort_fields, default_sort)
    page_obj = Paginator(stable_order(queryset, sort_by), page_size).get_page(request.GET.get("page"))

    return FilterSortPage(page_obj=page_obj, current_sort=sort_by)


def bulk_approve(  # type: ignore[explicit-any]
    request: HttpRequest,
    *,
    field_name: str,
    queryset: QuerySet,  # type: ignore[type-arg]
    redirect_url: str,
    item_label: str,
    on_item: Callable[..., None],
) -> HttpResponse:
    """Extract IDs from POST, apply per-item approval, redirect with message.

    The caller provides a pre-filtered *queryset* and an *on_item* callback
    that mutates and saves each instance.
    """
    raw_ids = request.POST.getlist(field_name)

    if not raw_ids:
        messages.info(request, f"No {item_label}s selected for approval")
        return redirect(redirect_url)

    valid_ids = [number for raw_id in raw_ids if (number := optional_int(raw_id)) is not None]

    if not valid_ids:
        messages.warning(request, f"No valid {item_label} IDs provided")
        return redirect(redirect_url)

    with transaction.atomic():
        items = queryset.select_for_update(of=("self",)).filter(id__in=valid_ids)
        count = 0
        for item in items:
            on_item(item)
            count += 1

    if count > 0:
        messages.success(request, f"Successfully approved {count} {item_label}(s)")
    else:
        messages.info(request, f"No unapproved {item_label}s found to approve")

    return redirect(redirect_url)


def role_sync_summary(stats: SyncRolesResult, *, dry_run: bool) -> str:
    """One-line role sync result, shared by the bot (ops channel) and the web Sync Roles page.

    Lives in core, not bot/, so web code doesn't import the bot package.
    """
    added, removed = ("would be added", "would be removed") if dry_run else ("added", "removed")
    return (
        f"{'[DRY RUN] ' if dry_run else ''}Role sync complete: {stats['roles_added']} {added}, "
        f"{stats['roles_removed']} {removed}, {stats['errors']} errors"
    )


# Discord IDs the app can't work without (setting name -> env var); each defaults to 0, which silently breaks features.
# DISCORD_LINK_CHANNEL_ID / DISCORD_WELCOME_CHANNEL_ID are optional: 0 turns those panels off.
REQUIRED_DISCORD_SETTINGS: dict[str, str] = {
    "COMPETITION_GUILD_ID": "DISCORD_GUILD_ID",
    "VOLUNTEER_GUILD_ID": "VOLUNTEER_GUILD_ID",
    "DISCORD_LOG_CHANNEL_ID": "DISCORD_LOG_CHANNEL_ID",
    "DISCORD_TICKET_QUEUE_CHANNEL_ID": "DISCORD_TICKET_QUEUE_CHANNEL_ID",
    "DISCORD_ANNOUNCEMENT_CHANNEL_ID": "DISCORD_ANNOUNCEMENT_CHANNEL_ID",
    "BLUETEAM_ROLE_ID": "BLUETEAM_ROLE_ID",
    "BLACKTEAM_ROLE_ID": "BLACKTEAM_ROLE_ID",
    "WHITETEAM_ROLE_ID": "WHITETEAM_ROLE_ID",
    "ORANGETEAM_ROLE_ID": "ORANGETEAM_ROLE_ID",
    "REDTEAM_ROLE_ID": "REDTEAM_ROLE_ID",
    "GOLDTEAM_ROLE_ID": "GOLDTEAM_ROLE_ID",
}


def missing_discord_settings() -> list[str]:
    """Environment variable names of required Discord IDs that are unset (0)."""
    return [env for name, env in REQUIRED_DISCORD_SETTINGS.items() if not getattr(settings, name, 0)]
