"""The chat process, `python -m chat`: the Discord runner, consuming the Discord backend's queued tasks
(core.task_backends). `python -m core.health chat` is its liveness check.

A crash ends the process, and its container restarts.
"""

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

logger = logging.getLogger(__name__)


def main() -> None:
    # Zulip's settings exist ahead of its runner; refused rather than run with nothing consuming Zulip's queued tasks
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
        # log_handler=None: discord.py's logs go through Django's console handler instead of a handler of its own
        PortalBot().run(settings.DISCORD_BOT_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
