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

# The heartbeat each loop writes
TIMER = "timer"
GROUPS = "groups"
QUOTIENT = "quotient"
QUEUE_WORKER = "queue-worker"
QUEUE_DISCORD = "queue-discord"
DASHBOARD = "dashboard"
QUEUE_ZULIP = "queue-zulip"
EVENTS_ZULIP = "events-zulip"

# Allowed age per heartbeat, several missed passes each. The worker: competition timer every 60s, group refresh every
# 5 min, Quotient sync every 5 min (every 15 min while backing off, so two backed-off passes), its queue polls every
# 2s. The chat process's Discord runner: its queue polls every 2s, the dashboard every 10s. Its Zulip runner: its
# queue polls every 2s, and its event long-poll answers within about a minute (Zulip sends a heartbeat event when
# nothing happened; the client gives up after 120s), so several missed polls.
BUDGETS: dict[str, int] = {
    TIMER: 300,
    GROUPS: 900,
    QUOTIENT: 1800,
    QUEUE_WORKER: 60,
    QUEUE_DISCORD: 60,
    DASHBOARD: 120,
    QUEUE_ZULIP: 60,
    EVENTS_ZULIP: 600,
}
WORKER_HEARTBEATS: tuple[str, ...] = (TIMER, GROUPS, QUOTIENT, QUEUE_WORKER)
DISCORD_HEARTBEATS: tuple[str, ...] = (QUEUE_DISCORD, DASHBOARD)
ZULIP_HEARTBEATS: tuple[str, ...] = (QUEUE_ZULIP, EVENTS_ZULIP)


def discord_configured(token: str | None) -> bool:
    """Whether Discord is configured, from DISCORD_BOT_TOKEN: the one rule core.task_backends.discord_enabled (through
    settings) and budgets (through the environment, as this module can't import Django) both apply."""
    return bool(token)


def zulip_configured(site: str | None, email: str | None, api_key: str | None) -> bool:
    """Whether Zulip is configured, from ZULIP_SITE, ZULIP_BOT_EMAIL and ZULIP_API_KEY: the one rule
    core.task_backends.zulip_enabled (through settings) and budgets (through the environment) both apply."""
    return bool(site and email and api_key)


def _always() -> bool:
    return True


_current_loop: ContextVar[tuple[str, Callable[[], bool]] | None] = ContextVar("heartbeat_loop", default=None)


def heartbeat_dir() -> Path:
    return Path(os.environ.get("BOT_HEARTBEAT_DIR", DEFAULT_DIR))


def budgets(process: str) -> dict[str, int]:
    """The heartbeats process ("worker" or "chat") writes, with their allowed age in seconds."""
    match process:
        case "worker":
            names = WORKER_HEARTBEATS
        case "chat":
            env = os.environ.get
            discord = DISCORD_HEARTBEATS if discord_configured(env("DISCORD_BOT_TOKEN")) else ()
            zulip = zulip_configured(env("ZULIP_SITE"), env("ZULIP_BOT_EMAIL"), env("ZULIP_API_KEY"))
            names = (*discord, *(ZULIP_HEARTBEATS if zulip else ()))
        case _:
            raise ValueError(f"Unknown process {process!r}, expected 'worker' or 'chat'")
    return {name: BUDGETS[name] for name in names}


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
