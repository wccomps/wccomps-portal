"""The worker process, `python -m worker`: the competition timer, the Authentik group refresh, the Quotient sync and the
worker queue (core.worker_tasks), none of which talks to a chat system. `python -m core.health worker` is its
liveness check.

The loops run under one TaskGroup, so a loop that crashes ends the process and the liveness probe's restart brings
every loop back.
"""

import asyncio
import logging
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")
django.setup()

from django.core.exceptions import ImproperlyConfigured
from portal.required import missing_settings

from core.heartbeat import beat, beat_for
from core.worker_tasks import WorkerQueueProcessor
from worker.jobs import competition_timer_loop, group_refresh_loop, quotient_sync_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


async def run() -> None:
    queue = WorkerQueueProcessor(on_start=beat_for, on_pass=beat)
    async with asyncio.TaskGroup() as group:
        group.create_task(competition_timer_loop())
        group.create_task(group_refresh_loop())
        group.create_task(quotient_sync_loop())
        group.create_task(queue.run())


def main() -> None:
    if missing := missing_settings():
        raise ImproperlyConfigured(f"{', '.join(missing)} not set; the worker needs them")
    logger.info("Worker starting")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")


if __name__ == "__main__":
    main()
