"""The chat process, `python -m chat`: a runner for each chat system this deployment configures (core.task_backends),
each consuming its own backend's queued tasks. `python -m core.health chat` is its liveness check.

The runners share one TaskGroup, so a runner that crashes ends the process and the liveness probe's restart brings
every runner back.
"""

import asyncio
import logging
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")
django.setup()

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from portal.required import missing_settings

from bot.client import PortalBot
from core.task_backends import discord_enabled, zulip_enabled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


async def run_discord() -> None:
    # start(), not run(): run() would install discord.py's own log handler, and its logs go through Django's console
    # handler instead. Leaving the context closes the client, as run() does.
    bot = PortalBot()
    async with bot:
        await bot.start(settings.DISCORD_BOT_TOKEN)


async def run() -> None:
    async with asyncio.TaskGroup() as group:
        group.create_task(run_discord())


def main() -> None:
    # Zulip's settings exist ahead of its runner; refused rather than half-run, since the liveness check expects the
    # Zulip heartbeats once its settings are set (core.heartbeat.budgets).
    if zulip_enabled():
        logger.error("Zulip is configured, but this release has no Zulip runner yet; unset ZULIP_* to start")
        sys.exit(1)
    if not discord_enabled():
        logger.error("No chat backend configured: set DISCORD_BOT_TOKEN")
        sys.exit(1)
    if missing := missing_settings():
        raise ImproperlyConfigured(f"{', '.join(missing)} not set; the chat process needs them")

    logger.info("Chat process starting")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
