"""The chat process, `python -m chat`: a runner per configured chat backend (core.heartbeat.CHAT_BACKENDS), together
in one event loop, each consuming its backend's queued tasks (core.task_backends). `python -m core.health chat` is its
liveness check.

A crash in any runner ends the process.
"""

import asyncio
import logging
import os
import sys
from collections.abc import Callable, Coroutine

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")
django.setup()

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from portal.required import missing_settings

from chat.zulip import runner as zulip_runner
from chat.zulip.client import ZulipClient
from core.heartbeat import CHAT_BACKENDS
from core.task_backends import TaskBackend, configured_chat_backends

logger = logging.getLogger(__name__)

# A backend's runner, set up but not started
type Runner = Callable[[], Coroutine[None, None, None]]


def main() -> None:
    backends = configured_chat_backends()
    if not backends:
        wanted = "; or ".join(f"{', '.join(b.settings)} for {TaskBackend(b.name).label}" for b in CHAT_BACKENDS)
        logger.error(f"No chat backend configured: set {wanted}")
        sys.exit(1)
    if missing := missing_settings():
        raise ImproperlyConfigured(f"{', '.join(missing)} not set; the chat process needs them")
    try:
        runners = [RUNNERS[backend]() for backend in backends]
    except ValueError as e:
        logger.error(f"Chat process not started: {e}")
        sys.exit(1)

    logger.info(f"Chat process starting: {', '.join(backend.label for backend in backends)}")
    try:
        asyncio.run(run(runners))
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


async def run(runners: list[Runner]) -> None:
    """The runners, until one of them crashes (which stops the rest)."""
    async with asyncio.TaskGroup() as group:
        for runner in runners:
            group.create_task(runner())


def _discord() -> Runner:
    # Imported here: a deployment without Discord never loads discord.py or the bot
    from bot.client import PortalBot

    async def run_bot() -> None:
        # As discord.py's Client.run does, without the log handler of its own run() would add: its logs go through
        # Django's console handler instead
        bot = PortalBot()
        async with bot:
            await bot.start(settings.DISCORD_BOT_TOKEN)

    return run_bot


def _zulip() -> Runner:
    # Built before any runner starts, so a ZULIP_SITE the client refuses (ValueError) stops the process first
    client = ZulipClient(settings.ZULIP_SITE, settings.ZULIP_BOT_EMAIL, settings.ZULIP_API_KEY)

    async def run_zulip() -> None:
        try:
            await zulip_runner.run(client)
        finally:
            await client.aclose()

    return run_zulip


# Each chat backend's runner setup, which raises ValueError for a setting it refuses
RUNNERS: dict[TaskBackend, Callable[[], Runner]] = {TaskBackend.DISCORD: _discord, TaskBackend.ZULIP: _zulip}


if __name__ == "__main__":
    main()
