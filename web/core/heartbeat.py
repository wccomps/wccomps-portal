"""Heartbeat files written by the worker's and chat backends' loops, read by `python -m core.health <process>`.

A loop's heartbeat advances only when a unit of its work completes, so a loop stuck on a dead
connection goes stale, and the health check (a k8s liveness probe) gets the pod restarted.

Deliberately free of Django imports so the probe can never hang on the database it observes.
"""

import logging
import os
import time
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DIR = "/app/run/heartbeat"

# Allowed age per loop, several missed passes each. The worker: competition timer every 60s, group refresh every
# 5 min, Quotient sync every 5 min (every 15 min while backing off, so two backed-off passes), queue polls every 2s.
WORKER_BUDGETS: dict[str, int] = {"timer": 300, "groups": 900, "quotient": 1800, "queue-worker": 60}
# The chat backends: each queue polls every 2s, the Discord dashboard every 10s, Zulip's event queue long-polls.
DISCORD_BUDGETS: dict[str, int] = {"queue-discord": 60, "dashboard": 120}
ZULIP_BUDGETS: dict[str, int] = {"queue-zulip": 60, "events-zulip": 300}


def _always() -> bool:
    return True


_current_loop: ContextVar[tuple[str, Callable[[], bool]] | None] = ContextVar("heartbeat_loop", default=None)


def heartbeat_dir() -> Path:
    return Path(os.environ.get("BOT_HEARTBEAT_DIR", DEFAULT_DIR))


def budgets(process: str) -> dict[str, int]:
    """The heartbeats process ("worker" or "chat") writes, with their allowed age in seconds.

    A chat backend counts when its credentials are in the environment, as core.task_backends decides from the same
    variables through settings (read here from the environment, since this module can't import Django).
    """
    match process:
        case "worker":
            return dict(WORKER_BUDGETS)
        case "chat":
            found: dict[str, int] = {}
            if os.environ.get("DISCORD_BOT_TOKEN"):
                found |= DISCORD_BUDGETS
            if all(os.environ.get(name) for name in ("ZULIP_SITE", "ZULIP_BOT_EMAIL", "ZULIP_API_KEY")):
                found |= ZULIP_BUDGETS
            return found
    raise ValueError(f"Unknown process {process!r}, expected 'worker' or 'chat'")


def record(name: str, alive: Callable[[], bool] = _always) -> None:
    """Record a completed unit of work, unless alive() says the loop's client is gone. Never raises; a failed write
    must not break the loop.

    Discord loops pass `lambda: not bot.is_closed()`. Not is_ready(): that flag isn't cleared by a transient
    websocket drop, so it says nothing useful about gateway health.
    """
    if not alive():
        return
    try:
        directory = heartbeat_dir()
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{name}.tmp"
        tmp.write_text(str(time.time()))
        tmp.replace(directory / name)
    except OSError as e:
        logger.debug(f"Could not write {name} heartbeat: {e}")


def beat_for(name: str, alive: Callable[[], bool] = _always) -> None:
    """Make beat() in this asyncio task, and code it awaits, advance name's heartbeat."""
    _current_loop.set((name, alive))


def beat() -> None:
    """Advance the heartbeat of the loop running this code, after a unit of work completes; outside one, nothing."""
    if current := _current_loop.get():
        record(*current)
