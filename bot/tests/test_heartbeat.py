"""The bot's loops beat their core.heartbeat heartbeats, and `python -m core.health chat` checks them."""

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import heartbeat
from core.health import check

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def hb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path / "hb"))
    return tmp_path / "hb"


def _open_bot() -> MagicMock:
    bot = MagicMock()
    bot.is_closed.return_value = False
    return bot


@pytest.fixture
def discord_only(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "token")


def test_the_chat_check_covers_the_loops_the_bot_runs(discord_only):
    """The worker process runs the queue-worker, timer, groups and quotient loops."""
    assert heartbeat.budgets("chat") == {heartbeat.QUEUE_DISCORD: 60, heartbeat.DASHBOARD: 120}


def test_check_passes_when_all_fresh(hb_dir, discord_only):
    for name in heartbeat.budgets("chat"):
        heartbeat.record(name)

    assert check("chat") == []


def test_check_reports_stale_and_missing(hb_dir, discord_only):
    heartbeat.record("queue-discord")
    old = time.time() - heartbeat.budgets("chat")["queue-discord"] - 10
    (hb_dir / "queue-discord").write_text(str(old))

    problems = check("chat")

    assert any(p.startswith("dashboard:") and "missing" in p for p in problems)
    assert any(p.startswith("queue-discord:") and "stale" in p for p in problems)


def test_chat_health_module_exit_codes(hb_dir, discord_only):
    env = {**os.environ, "BOT_HEARTBEAT_DIR": str(hb_dir), "PYTHONPATH": str(REPO_ROOT / "web")}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    probe = [sys.executable, "-m", "core.health", "chat"]

    # Fixed command running this interpreter, not untrusted input
    missing = subprocess.run(probe, env=env, capture_output=True, text=True, cwd=REPO_ROOT)  # noqa: S603
    for name in heartbeat.budgets("chat"):
        heartbeat.record(name)
    fresh = subprocess.run(probe, env=env, capture_output=True, text=True, cwd=REPO_ROOT)  # noqa: S603

    assert missing.returncode == 1 and "missing" in missing.stdout
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr


def test_a_closed_bot_stops_its_loops_beating(hb_dir):
    bot = _open_bot()
    bot.is_closed.return_value = True

    heartbeat.record("queue-discord", lambda: not bot.is_closed())

    assert not (hb_dir / "queue-discord").exists()


@pytest.fixture
def beats(monkeypatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(heartbeat, "record", lambda name, alive: recorded.append(name))
    return recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("work_fails", [False, True])
async def test_the_dashboard_beats_only_after_a_successful_pass(work_fails, beats, monkeypatch):
    from bot import unified_dashboard

    dashboard = unified_dashboard.UnifiedDashboard(_open_bot())
    dashboard.bot.wait_until_ready = AsyncMock()

    async def work() -> None:
        dashboard.running = False
        if work_fails:
            raise RuntimeError("db down")

    monkeypatch.setattr(unified_dashboard, "recycle_db_connection", AsyncMock())
    monkeypatch.setattr(dashboard, "_pass", work)
    monkeypatch.setattr(unified_dashboard.asyncio, "sleep", AsyncMock())
    dashboard.running = True

    await dashboard._dashboard_loop()

    assert beats == ([] if work_fails else ["dashboard"])


def _queue(monkeypatch, pending: list[object], handle) -> object:
    from bot import discord_queue
    from core import task_queue

    processor = discord_queue.DiscordQueueProcessor(_open_bot())
    monkeypatch.setattr(task_queue, "recycle_db_connection", AsyncMock())
    monkeypatch.setattr(processor, "recover_stranded", AsyncMock())
    monkeypatch.setattr(processor, "_process_task", handle)

    batches = iter([pending])
    monkeypatch.setattr(
        task_queue,
        "sync_to_async",
        lambda func: (lambda: _first(batches)) if func.__name__ == "get_pending_tasks" else func,
    )
    processor.QUEUE_POLL_INTERVAL_SECONDS = 0.01
    return processor


async def _first(batches) -> list[object]:
    return next(batches, [])


@pytest.mark.asyncio
async def test_the_queue_beats_after_each_poll_and_each_task(beats, monkeypatch):
    """A batch of tasks that together outlast the budget keeps the loop alive, one finished task at a time."""
    during: list[int] = []

    async def handle(task: object) -> None:
        during.append(len(beats))

    processor = _queue(monkeypatch, ["a", "b", "c"], handle)
    loop = asyncio.ensure_future(processor.run())
    await asyncio.sleep(0.2)
    loop.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop

    assert during == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_wedged_task_stops_the_queue_heartbeat(beats, monkeypatch):
    """A task stuck on the shared thread (a recv on a half-open DB socket) goes stale instead of looking alive."""
    release = asyncio.Event()

    async def handle(task: object) -> None:
        await release.wait()

    processor = _queue(monkeypatch, ["stuck"], handle)
    loop = asyncio.ensure_future(processor.run())
    await asyncio.sleep(0.3)
    wedged = list(beats)
    loop.cancel()
    release.set()
    with contextlib.suppress(asyncio.CancelledError):
        await loop

    assert wedged == ["queue-discord"]


@pytest.mark.asyncio
async def test_a_cancelled_loop_stops_beating(beats, monkeypatch):
    processor = _queue(monkeypatch, [], AsyncMock())
    processor.start()
    await asyncio.sleep(0.1)
    assert beats
    processor.stop()
    await asyncio.sleep(0.05)
    after_stop = len(beats)

    await asyncio.sleep(0.1)

    assert len(beats) == after_stop
