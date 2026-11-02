"""Heartbeat files and the `python -m bot.health` liveness check (k8s prep, W1 detection half)."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import heartbeat
from bot.health import check

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def hb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path / "hb"))
    return tmp_path / "hb"


def _open_bot() -> MagicMock:
    bot = MagicMock()
    bot.is_closed.return_value = False
    return bot


def test_record_writes_timestamp(hb_dir):
    heartbeat.record("queue", _open_bot())

    assert abs(float((hb_dir / "queue").read_text()) - time.time()) < 5


def test_record_skipped_when_bot_closed(hb_dir):
    bot = _open_bot()
    bot.is_closed.return_value = True

    heartbeat.record("queue", bot)

    assert not (hb_dir / "queue").exists()


def test_record_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(blocker / "hb"))  # can't create a dir under a file

    heartbeat.record("queue", _open_bot())  # must not raise


def test_check_passes_when_all_fresh(hb_dir):
    for name in heartbeat.BUDGET_SECONDS:
        heartbeat.record(name, _open_bot())

    assert check() == []


def test_check_reports_stale_and_missing(hb_dir):
    heartbeat.record("dashboard", _open_bot())
    heartbeat.record("timer", _open_bot())
    old = time.time() - heartbeat.BUDGET_SECONDS["timer"] - 10
    (hb_dir / "timer").write_text(str(old))

    problems = check()

    assert any(p.startswith("queue:") and "missing" in p for p in problems)
    assert any(p.startswith("timer:") and "stale" in p for p in problems)
    assert not any(p.startswith("dashboard:") for p in problems)


def test_health_module_exit_codes_and_no_django(hb_dir):
    env = {**os.environ, "BOT_HEARTBEAT_DIR": str(hb_dir), "PYTHONPATH": str(REPO_ROOT)}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    probe = [sys.executable, "-c", "import runpy,sys; runpy.run_module('bot.health', run_name='__main__')"]

    # Fixed command running this interpreter, not untrusted input
    missing = subprocess.run(probe, env=env, capture_output=True, text=True, cwd=REPO_ROOT)  # noqa: S603
    for name in heartbeat.BUDGET_SECONDS:
        heartbeat.record(name, _open_bot())
    fresh = subprocess.run(  # noqa: S603
        [*probe[:2], probe[2] + "; import sys; assert 'django' not in sys.modules"],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert missing.returncode == 1 and "missing" in missing.stdout
    assert fresh.returncode == 0, fresh.stdout + fresh.stderr


@pytest.fixture
def beats(monkeypatch) -> list[str]:
    recorded: list[str] = []
    monkeypatch.setattr(heartbeat, "record", lambda name, bot: recorded.append(name))
    return recorded


@pytest.mark.asyncio
async def test_beat_outside_a_loop_records_nothing(beats):
    """sync_roles also runs from the group refresh, which has no heartbeat."""
    heartbeat.beat()

    assert beats == []


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
    monkeypatch.setattr(dashboard, "_check_and_update", work)
    monkeypatch.setattr(dashboard, "_initialize_dashboard", AsyncMock())
    monkeypatch.setattr(unified_dashboard.asyncio, "sleep", AsyncMock())
    dashboard.running = True

    await dashboard._dashboard_loop()

    assert beats == ([] if work_fails else ["dashboard"])


def _queue(monkeypatch, pending: list[object], handle) -> object:
    from bot import discord_queue

    processor = discord_queue.DiscordQueueProcessor(_open_bot())
    monkeypatch.setattr(discord_queue, "recycle_db_connection", AsyncMock())
    monkeypatch.setattr(processor, "_recover_stranded_tasks", AsyncMock())
    monkeypatch.setattr(processor, "_process_task", handle)

    batches = iter([pending])
    monkeypatch.setattr(
        discord_queue,
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
    processor.running = True
    loop = asyncio.ensure_future(processor._process_loop())
    await asyncio.sleep(0.2)
    processor.running = False
    await loop

    assert during == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_wedged_task_stops_the_queue_heartbeat(beats, monkeypatch):
    """A task stuck on the shared thread (a recv on a half-open DB socket) goes stale instead of looking alive."""
    release = asyncio.Event()

    async def handle(task: object) -> None:
        await release.wait()

    processor = _queue(monkeypatch, ["stuck"], handle)
    processor.running = True
    loop = asyncio.ensure_future(processor._process_loop())
    await asyncio.sleep(0.3)
    wedged = list(beats)
    processor.running = False
    release.set()
    await loop

    assert wedged == ["queue"]


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
