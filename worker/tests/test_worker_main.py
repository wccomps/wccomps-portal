"""`python -m worker`: runs the periodic jobs and the worker queue, with no chat client anywhere."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_worker_imports_no_discord():
    code = "import django; django.setup(); import sys, worker.jobs, worker.__main__; print('discord' in sys.modules)"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "web"), str(REPO_ROOT)])}
    env.setdefault("DJANGO_SETTINGS_MODULE", "portal.test_settings")
    # Fixed command running this interpreter, not untrusted input
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env, cwd=REPO_ROOT
    )
    assert out.stdout.strip().splitlines()[-1] == "False", out.stdout + out.stderr


def test_the_worker_refuses_to_start_without_required_settings(monkeypatch, settings):
    from worker import __main__ as worker_main

    settings.ORG_NAME = ""
    monkeypatch.setattr(worker_main.asyncio, "run", lambda _coro: pytest.fail("the worker started"))

    with pytest.raises(ImproperlyConfigured, match="^ORG_NAME not set; the worker refuses to start"):
        worker_main.main()


def test_the_worker_refuses_to_start_with_a_weak_secret_key(monkeypatch):
    from worker import __main__ as worker_main

    monkeypatch.setenv("DJANGO_SECRET_KEY", "changeme")
    monkeypatch.setattr(worker_main.asyncio, "run", lambda _coro: pytest.fail("the worker started"))

    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY is too weak"):
        worker_main.main()


class _Queue:
    def __init__(self, **_hooks: object) -> None:
        pass

    async def run(self) -> None:
        await asyncio.Event().wait()


async def _forever() -> None:
    await asyncio.Event().wait()


async def test_a_crashing_loop_ends_the_worker(monkeypatch):
    """A worker that exits restarts with its container; one left running without its group refresh never would."""
    from worker import __main__ as worker_main

    async def crash() -> None:
        raise RuntimeError("loop crashed")

    monkeypatch.setattr(worker_main, "WorkerQueueProcessor", _Queue)
    monkeypatch.setattr(worker_main, "competition_timer_loop", _forever)
    monkeypatch.setattr(worker_main, "quotient_sync_loop", _forever)
    monkeypatch.setattr(worker_main, "session_cleanup_loop", _forever)
    monkeypatch.setattr(worker_main, "group_refresh_loop", crash)

    with pytest.raises(ExceptionGroup) as ended:
        await asyncio.wait_for(worker_main.run(), timeout=5)

    assert ended.group_contains(RuntimeError, match="loop crashed")
