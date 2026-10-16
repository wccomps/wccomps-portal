import ipaddress
import json
import logging
import queue
import threading
from collections.abc import Callable, Collection, Iterator
from datetime import datetime
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib import messages
from django.core.paginator import Page, Paginator
from django.db import connection
from django.db.models import Model, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect

from core.discord_tasks import SyncRolesResult

logger = logging.getLogger(__name__)


def client_ip(request: HttpRequest) -> str:
    """The real client address: Cloudflare's CF-Connecting-IP, else the connecting peer.

    Behind the tunnel REMOTE_ADDR is always the proxy. The header is trustworthy only because every
    production request comes through Cloudflare, which overwrites it; served directly, clients can set it.
    """
    header = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    try:
        return str(ipaddress.ip_address(header))
    except ValueError:
        return request.META.get("REMOTE_ADDR") or "0.0.0.0"  # noqa: S104 - an address value, not a bind


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


def ndjson_progress(step: str, current: int, total: int, ok: bool = True) -> str:
    """Encode a single progress line as newline-delimited JSON for streaming views."""
    return json.dumps({"step": step, "current": current, "total": total, "ok": ok}) + "\n"


def ndjson_done(message: str, success: bool = True) -> str:
    """Encode the final line of a streamed operation, which the page shows as the outcome."""
    return json.dumps({"done": True, "success": success, "message": message}) + "\n"


def run_detached(lines: Iterator[str]) -> Iterator[str]:
    """Run a streamed operation to the end in its own thread, relaying its lines while the client listens.

    A streaming view's generator stops at its next line once the client goes away (a closed tab, a proxy
    timeout), leaving the operation half done. Here the client only stops the relay; the work finishes.
    """
    relay: queue.Queue[str | None] = queue.Queue()

    def run() -> None:
        try:
            for line in lines:
                relay.put(line)
        except Exception as e:
            logger.exception("Streamed operation failed")
            relay.put(ndjson_done(f"Failed: {e}", success=False))
        finally:
            # This thread's own DB connection; nothing else would close it
            connection.close()
            relay.put(None)

    # Not a daemon: a worker shutting down (a deploy) waits for the operation, up to gunicorn's graceful timeout
    threading.Thread(target=run, name="run-detached", daemon=False).start()
    while (line := relay.get()) is not None:
        yield line


def optional_int(value: str) -> int | None:
    """The integer in request text, or None when the text isn't one."""
    try:
        return int(value)
    except ValueError:
        return None


class FilterSortPage[M: Model](TypedDict):
    page_obj: Page[M]
    current_sort: str


def requested_sort(request: HttpRequest, sort_fields: Collection[str], default_sort: str) -> str:
    """The list's ?sort=: one of sort_fields, with a leading - for descending; "default" means no ordering, and any
    other value the default."""
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

    items = queryset.filter(id__in=valid_ids)
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
    from django.conf import settings

    return [env for name, env in REQUIRED_DISCORD_SETTINGS.items() if not getattr(settings, name, 0)]
