"""Heartbeat files written by the bot's queue, dashboard and timer loops, read by `python -m bot.health`.

A loop's heartbeat advances only when a unit of its work completes, so a loop stuck on a dead
connection goes stale, and the health check (a k8s liveness probe) gets the pod restarted.

Deliberately free of Django imports so the probe can never hang on the database it observes.
"""

import logging
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_DIR = "/app/run/heartbeat"

# Allowed age per loop, several missed passes each: queue polls every 2s, dashboard every 10s,
# competition timer every 60s.
BUDGET_SECONDS: dict[str, int] = {"queue": 60, "dashboard": 120, "timer": 300}


class _ClosableBot(Protocol):
    def is_closed(self) -> bool: ...


_current_loop: ContextVar[tuple[str, _ClosableBot] | None] = ContextVar("heartbeat_loop", default=None)


def heartbeat_dir() -> Path:
    return Path(os.environ.get("BOT_HEARTBEAT_DIR", DEFAULT_DIR))


def record(name: str, bot: _ClosableBot) -> None:
    """Record a completed unit of work. Never raises; a failed write must not break the loop.

    Gated on the client not being closed. Not on is_ready(): that flag isn't cleared by a
    transient websocket drop, so it says nothing useful about gateway health.
    """
    if bot.is_closed():
        return
    try:
        directory = heartbeat_dir()
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{name}.tmp"
        tmp.write_text(str(time.time()))
        tmp.replace(directory / name)
    except OSError as e:
        logger.debug(f"Could not write {name} heartbeat: {e}")


def beat_for(name: str, bot: _ClosableBot) -> None:
    """Make beat() in this asyncio task, and code it awaits, advance name's heartbeat."""
    _current_loop.set((name, bot))


def beat() -> None:
    """Advance the heartbeat of the loop running this code, after a unit of work completes; outside one, nothing."""
    if current := _current_loop.get():
        record(*current)
