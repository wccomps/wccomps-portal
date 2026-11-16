"""The chat process, `python -m chat`: a runner per configured chat backend (Discord, Zulip), together in one event
loop, each consuming its backend's queued tasks (core.task_backends). `python -m core.health chat` is its liveness
check.

A crash in either runner ends the process, and its container restarts.
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
from chat.zulip import runner as zulip_runner
from chat.zulip.client import ZulipClient
from core.task_backends import discord_enabled, zulip_enabled

logger = logging.getLogger(__name__)


def main() -> None:
    discord, zulip = discord_enabled(), zulip_enabled()
    if not discord and not zulip:
        logger.error(
            "No chat backend configured: set DISCORD_BOT_TOKEN for Discord, or ZULIP_SITE, ZULIP_BOT_EMAIL and "
            "ZULIP_API_KEY for Zulip"
        )
        sys.exit(1)
    if missing := missing_settings():
        raise ImproperlyConfigured(f"{', '.join(missing)} not set; the chat process needs them")

    backends = [name for name, on in (("Discord", discord), ("Zulip", zulip)) if on]
    logger.info(f"Chat process starting: {', '.join(backends)}")
    try:
        asyncio.run(run(discord=discord, zulip=zulip))
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


async def run(*, discord: bool, zulip: bool) -> None:
    """The configured runners, until one of them crashes (which stops the other)."""
    async with asyncio.TaskGroup() as group:
        if discord:
            group.create_task(_run_discord())
        if zulip:
            group.create_task(_run_zulip())


async def _run_discord() -> None:
    # As discord.py's Client.run does, without the log handler of its own run() would add: its logs go through
    # Django's console handler instead
    bot = PortalBot()
    async with bot:
        await bot.start(settings.DISCORD_BOT_TOKEN)


async def _run_zulip() -> None:
    client = ZulipClient(settings.ZULIP_SITE, settings.ZULIP_BOT_EMAIL, settings.ZULIP_API_KEY)
    try:
        await zulip_runner.run(client)
    finally:
        await client.aclose()


if __name__ == "__main__":
    main()
