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
# Well under the events-zulip budget (core.heartbeat), which a backed-off loop beats after each wait
MAX_BACKOFF_SECONDS = 120


async def run(client: ZulipClient) -> None:
    """Run the queue processor and the event loop until cancelled; either one crashing ends both."""
    async with asyncio.TaskGroup() as group:
        group.create_task(ZulipQueueProcessor(client).run())
        group.create_task(event_loop(client))


async def event_loop(client: ZulipClient) -> None:
    """Poll and handle events until cancelled. A queue lost after a poll was answered is registered again at once;
    one lost before its first poll is answered, or any other failure, makes the loop wait first, each wait doubling up
    to MAX_BACKOFF_SECONDS. events-zulip beats after each handled poll or waited-out failure, so only a stuck loop goes
    stale."""
    queue: tuple[str, int] | None = None
    backoff = FIRST_BACKOFF_SECONDS
    while True:
        fresh = queue is None
        try:
            if queue is None:
                queue = await client.register_queue()
                logger.info("Registered a Zulip event queue")
            queue_id, last_event_id = queue
            events = await client.get_events(queue_id, last_event_id)
        except ZulipError as e:
            if e.code == BAD_EVENT_QUEUE_ID:
                queue = None
                if not fresh:
                    logger.info("Zulip lost the event queue; registering a new one")
                    continue
            logger.warning(f"Zulip refused the event queue request ({e}); retrying in {backoff}s")
            backoff = await _wait(backoff)
        except httpx.HTTPError as e:
            logger.warning(f"Could not reach Zulip for events ({e!r}); retrying in {backoff}s")
            backoff = await _wait(backoff)
        else:
            backoff = FIRST_BACKOFF_SECONDS
            queue = queue_id, await _handle(client, events, last_event_id)
        record(EVENTS_ZULIP)


async def _wait(backoff: int) -> int:
    """Sleep backoff seconds; the next wait, doubled up to MAX_BACKOFF_SECONDS."""
    await asyncio.sleep(backoff)
    return min(backoff * 2, MAX_BACKOFF_SECONDS)


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
