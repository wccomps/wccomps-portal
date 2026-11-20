"""The worker process, `python -m worker`: the competition timer, the Authentik group refresh, the Quotient sync, the
preflight report, the expired session cleanup and the worker queue (core.worker_tasks), none of which talks to a chat
system.
`python -m core.health worker` is its liveness check.

The loops run under one TaskGroup, so a loop that crashes ends the process, and its container restarts with every
loop.
"""

import asyncio
import logging
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")
django.setup()

from portal.required import refuse_to_start

from core.heartbeat import beat, beat_for
from core.worker_tasks import WorkerQueueProcessor
from worker.jobs import (
    competition_timer_loop,
    group_refresh_loop,
    preflight_report_loop,
    quotient_sync_loop,
    session_cleanup_loop,
)

logger = logging.getLogger(__name__)


async def run() -> None:
    queue = WorkerQueueProcessor(on_start=beat_for, on_pass=beat)
    async with asyncio.TaskGroup() as group:
        group.create_task(competition_timer_loop())
        group.create_task(group_refresh_loop())
        group.create_task(quotient_sync_loop())
        group.create_task(preflight_report_loop())
        group.create_task(session_cleanup_loop())
        group.create_task(queue.run())


def main() -> None:
    refuse_to_start("the worker")
    logger.info("Worker starting")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")


if __name__ == "__main__":
    main()
