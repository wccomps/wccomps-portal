"""Heartbeat files and the `python -m core.health worker|chat` liveness check."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core import health, heartbeat

REPO_ROOT = Path(__file__).resolve().parents[3]
CHAT_ENV = ("DISCORD_BOT_TOKEN", "ZULIP_SITE", "ZULIP_BOT_EMAIL", "ZULIP_API_KEY")


@pytest.fixture
def hb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path / "hb"))
    return tmp_path / "hb"


@pytest.fixture
def no_chat(monkeypatch):
    for name in CHAT_ENV:
        monkeypatch.delenv(name, raising=False)


def _zulip(monkeypatch) -> None:
    monkeypatch.setenv("ZULIP_SITE", "https://chat.example.org")
    monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.org")
    monkeypatch.setenv("ZULIP_API_KEY", "k")


def test_record_writes_timestamp(hb_dir):
    heartbeat.record("queue-worker")

    assert abs(float((hb_dir / "queue-worker").read_text()) - time.time()) < 5


def test_record_skipped_when_not_alive(hb_dir):
    heartbeat.record("queue-discord", lambda: False)

    assert not (hb_dir / "queue-discord").exists()


def test_record_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(blocker / "hb"))  # can't create a dir under a file

    heartbeat.record("queue-worker")  # must not raise


async def test_beat_advances_the_loop_bound_in_this_task(hb_dir):
    async def loop() -> None:
        heartbeat.beat_for("timer")
        heartbeat.beat()

    await asyncio.create_task(loop())

    assert (hb_dir / "timer").exists()


async def test_beat_honors_the_loops_alive_check(hb_dir):
    async def loop() -> None:
        heartbeat.beat_for("dashboard", lambda: False)
        heartbeat.beat()

    await asyncio.create_task(loop())

    assert not (hb_dir / "dashboard").exists()


def test_beat_outside_a_loop_records_nothing(hb_dir):
    """sync_roles also runs outside a loop with a heartbeat."""
    heartbeat.beat()

    assert not hb_dir.exists()


def test_worker_health_checks_worker_loops(hb_dir):
    for name in ("timer", "groups", "quotient", "queue-worker"):
        heartbeat.record(name)
    assert health.check("worker") == []
    (hb_dir / "groups").write_text(str(time.time() - 1000))
    assert health.check("worker") == ["groups: heartbeat stale (1000s old, budget 900s)"]


def test_worker_health_reports_a_missing_loop(hb_dir):
    for name in ("timer", "groups", "queue-worker"):
        heartbeat.record(name)

    assert health.check("worker") == [f"quotient: heartbeat missing ({hb_dir / 'quotient'})"]


def test_worker_budgets_ignore_chat_settings(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    _zulip(monkeypatch)

    assert heartbeat.budgets("worker") == {"timer": 300, "groups": 900, "quotient": 1800, "queue-worker": 60}


def test_chat_health_only_checks_configured_backends(no_chat, monkeypatch):
    _zulip(monkeypatch)
    assert sorted(heartbeat.budgets("chat")) == ["events-zulip", "queue-zulip"]


def test_chat_budgets_for_discord(no_chat, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")

    assert heartbeat.budgets("chat") == {"queue-discord": 60, "dashboard": 120}


def test_chat_budgets_for_both_backends(no_chat, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    _zulip(monkeypatch)

    assert sorted(heartbeat.budgets("chat")) == ["dashboard", "events-zulip", "queue-discord", "queue-zulip"]


@pytest.mark.parametrize("missing", ["ZULIP_SITE", "ZULIP_BOT_EMAIL", "ZULIP_API_KEY"])
def test_a_partly_configured_zulip_is_not_checked(no_chat, monkeypatch, missing):
    """Zulip runs only with all three credentials (core.task_backends.zulip_enabled), so only then does it beat."""
    _zulip(monkeypatch)
    monkeypatch.setenv(missing, "")

    assert heartbeat.budgets("chat") == {}


def test_unknown_process_is_refused():
    with pytest.raises(ValueError, match="Unknown process"):
        heartbeat.budgets("bot")


@pytest.mark.parametrize("argv", [[], ["bot"], ["worker", "chat"]])
def test_health_main_refuses_bad_arguments(argv, capsys):
    assert health.main(argv) == 2
    assert "usage" in capsys.readouterr().out


def _python(*args: str, hb_dir: Path) -> subprocess.CompletedProcess[str]:
    """This interpreter run from the repo root with web/ on the path and no Django settings, as the probe runs it."""
    env = {**os.environ, "BOT_HEARTBEAT_DIR": str(hb_dir), "PYTHONPATH": str(REPO_ROOT / "web")}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    # Fixed command running this interpreter, not untrusted input
    return subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, cwd=REPO_ROOT)  # noqa: S603


def test_health_module_exit_codes(hb_dir):
    missing = _python("-m", "core.health", "worker", hb_dir=hb_dir)
    for name in heartbeat.budgets("worker"):
        heartbeat.record(name)
    fresh = _python("-m", "core.health", "worker", hb_dir=hb_dir)

    assert missing.returncode == 1 and "missing" in missing.stdout
    assert fresh.returncode == 0 and fresh.stdout.strip() == "ok", fresh.stdout + fresh.stderr


def test_health_imports_no_django(hb_dir):
    """The probe must never hang on the database it observes."""
    out = _python("-c", "import sys, core.health; print('django' in sys.modules)", hb_dir=hb_dir)

    assert out.stdout.strip() == "False", out.stdout + out.stderr


async def test_a_queue_processor_beats_through_its_hooks(monkeypatch):
    """Wired as the worker process wires it, with no Discord client: each poll advances queue-worker."""
    from core import task_queue
    from core.worker_tasks import WorkerQueueProcessor

    beats: list[str] = []
    monkeypatch.setattr(heartbeat, "record", lambda name, alive: beats.append(name))
    processor = WorkerQueueProcessor(on_start=heartbeat.beat_for, on_pass=heartbeat.beat)
    monkeypatch.setattr(task_queue, "recycle_db_connection", AsyncMock())
    monkeypatch.setattr(processor, "recover_stranded", AsyncMock())
    monkeypatch.setattr(task_queue, "sync_to_async", lambda func: lambda: _no_rows())
    processor.QUEUE_POLL_INTERVAL_SECONDS = 0.01

    processor.start()
    await asyncio.sleep(0.05)
    processor.stop()

    assert beats and set(beats) == {"queue-worker"}


async def _no_rows() -> list[object]:
    return []
