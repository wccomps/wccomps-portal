"""The systems that consume queued tasks, and which chat systems this deployment has configured."""

from django.conf import settings
from django.db import models

from core.heartbeat import CHAT_BACKENDS


class TaskBackend(models.TextChoices):
    DISCORD = "discord", "Discord"
    ZULIP = "zulip", "Zulip"
    WORKER = "worker", "Worker"


def configured_chat_backends() -> list[TaskBackend]:
    """The chat backends whose settings core.heartbeat.CHAT_BACKENDS names are all set."""
    return [TaskBackend(b.name) for b in CHAT_BACKENDS if b.configured(_setting)]


def _setting(key: str) -> str | None:
    value: str | None = getattr(settings, key)
    return value


def discord_enabled() -> bool:
    return TaskBackend.DISCORD in configured_chat_backends()
