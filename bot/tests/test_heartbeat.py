"""Heartbeat files and the `python -m bot.health` liveness check (k8s prep, W1 detection half)."""

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "cls_name", "work_attr", "loop_attr", "beat"),
    [
        ("bot.discord_queue", "DiscordQueueProcessor", "_process_pending_tasks", "_process_loop", "queue"),
        ("bot.unified_dashboard", "UnifiedDashboard", "_pass", "_dashboard_loop", "dashboard"),
    ],
)
@pytest.mark.parametrize("work_fails", [False, True])
async def test_loops_beat_only_after_successful_pass(
    module, cls_name, work_attr, loop_attr, beat, work_fails, monkeypatch
):
    import importlib

    mod = importlib.import_module(module)
    obj = getattr(mod, cls_name).__new__(getattr(mod, cls_name))
    obj.bot = _open_bot()
    obj.bot.wait_until_ready = AsyncMock()
    beats: list[str] = []

    async def work(*_a: object) -> None:
        obj.running = False
        if work_fails:
            raise RuntimeError("db down")

    monkeypatch.setattr(mod, "recycle_db_connection", AsyncMock())
    monkeypatch.setattr(mod, "record_heartbeat", lambda name, bot: beats.append(name))
    monkeypatch.setattr(obj, work_attr, work)
    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
    obj.running = True

    await getattr(obj, loop_attr)()

    assert beats == ([] if work_fails else [beat])
