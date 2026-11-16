"""The chat process's Zulip runner: delivers the Zulip backend's queued tasks (ZulipQueueProcessor) and listens on a
Zulip event queue (a long poll, so no public webhook) for messages in ticket topics and their edits and deletes."""

import asyncio
import logging

import httpx

from chat.zulip.client import ZulipClient, ZulipError
from chat.zulip.incoming import handle_event
from chat.zulip.outgoing import ZulipQueueProcessor
from core.heartbeat import EVENTS_ZULIP, record
from core.utils import recycle_db_connection

logger = logging.getLogger(__name__)

# Zulip forgets a queue nobody polled for a while, or all of them when it restarts
BAD_EVENT_QUEUE_ID = "BAD_EVENT_QUEUE_ID"
FIRST_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120


async def run(client: ZulipClient) -> None:
    """Run the queue processor and the event loop until cancelled; either one crashing ends both."""
    async with asyncio.TaskGroup() as group:
        group.create_task(ZulipQueueProcessor(client).run())
        group.create_task(event_loop(client))


async def event_loop(client: ZulipClient) -> None:
    """Poll the event queue and handle each event, until cancelled. A lost queue is registered again at once; any
    other failure to register or poll is logged and retried after a wait that doubles up to MAX_BACKOFF_SECONDS.
    events-zulip advances after each poll Zulip answered, so a Zulip that stays unreachable lets it go stale."""
    queue: tuple[str, int] | None = None
    backoff = FIRST_BACKOFF_SECONDS
    while True:
        try:
            if queue is None:
                queue = await client.register_queue()
                logger.info("Registered a Zulip event queue")
            queue_id, last_event_id = queue
            events = await client.get_events(queue_id, last_event_id)
        except ZulipError as e:
            if e.code == BAD_EVENT_QUEUE_ID:
                logger.info("Zulip lost the event queue; registering a new one")
                queue = None
                continue
            logger.warning(f"Zulip refused the event queue request ({e}); retrying in {backoff}s")
        except httpx.HTTPError as e:
            logger.warning(f"Could not reach Zulip for events ({e!r}); retrying in {backoff}s")
        else:
            record(EVENTS_ZULIP)
            backoff = FIRST_BACKOFF_SECONDS
            queue = queue_id, await _handle(client, events, last_event_id)
            continue
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


async def _handle(client: ZulipClient, events: list[dict[str, object]], last_event_id: int) -> int:
    """Handle a poll's events in order; the last event id seen. An event that fails is logged and passed over, so
    one bad event never stops the rest."""
    await recycle_db_connection()
    for event in events:
        event_id = event.get("id")
        if type(event_id) is int:
            last_event_id = max(last_event_id, event_id)
        try:
            await handle_event(client, event)
        except Exception:
            logger.exception(f"Failed to handle Zulip event {event_id!r}")
    return last_event_id
