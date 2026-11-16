"""The systems that consume queued tasks, and which chat systems this deployment has configured."""

from django.conf import settings
from django.db import models


class TaskBackend(models.TextChoices):
    DISCORD = "discord", "Discord"
    ZULIP = "zulip", "Zulip"
    WORKER = "worker", "Worker"


def discord_enabled() -> bool:
    return bool(settings.DISCORD_BOT_TOKEN)


def zulip_enabled() -> bool:
    return bool(settings.ZULIP_SITE and settings.ZULIP_BOT_EMAIL and settings.ZULIP_API_KEY)
