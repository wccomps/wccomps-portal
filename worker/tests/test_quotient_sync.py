"""Quotient sync backs off and logs state changes only while Quotient is down."""

import logging
from unittest.mock import Mock, patch

import pytest

from worker import jobs
from worker.jobs import BACKOFF_MINUTES, SYNC_MINUTES
from worker.tests.loops import one_pass


@pytest.fixture(autouse=True)
def hb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path))
    return tmp_path


def _fail():
    raise ValueError("Failed to retrieve infrastructure from Quotient")


@pytest.mark.asyncio
async def test_first_failure_warns_once_and_backs_off(caplog):
    sync = jobs.QuotientSync()
    with (
        patch.object(jobs, "sync_quotient_metadata", _fail),
        caplog.at_level(logging.DEBUG, logger="worker.jobs"),
    ):
        await sync.run_pass()
        await sync.run_pass()
        await sync.run_pass()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert not [r for r in caplog.records if r.exc_info]
    assert sync.interval_minutes == BACKOFF_MINUTES


@pytest.mark.asyncio
async def test_quotient_unreachable_still_beats(hb_dir, monkeypatch):
    monkeypatch.setattr("worker.jobs.sync_quotient_metadata", Mock(side_effect=ValueError("Quotient down")))
    await one_pass(jobs.quotient_sync_loop, monkeypatch)
    assert (hb_dir / "quotient").exists()


@pytest.mark.asyncio
async def test_recovery_logs_once_and_restores_interval(caplog):
    sync = jobs.QuotientSync()
    sync.quotient_unavailable = True
    with (
        patch.object(jobs, "sync_quotient_metadata", lambda: None),
        caplog.at_level(logging.DEBUG, logger="worker.jobs"),
    ):
        await sync.run_pass()

    assert [r for r in caplog.records if r.levelno == logging.INFO and "reachable" in r.getMessage()]
    assert sync.interval_minutes == SYNC_MINUTES


@pytest.mark.asyncio
async def test_success_while_up_keeps_the_interval():
    sync = jobs.QuotientSync()
    with patch.object(jobs, "sync_quotient_metadata", lambda: None):
        await sync.run_pass()

    assert sync.interval_minutes == SYNC_MINUTES


@pytest.mark.asyncio
async def test_unexpected_error_keeps_traceback(caplog):
    def boom():
        raise RuntimeError("bug")

    with (
        patch.object(jobs, "sync_quotient_metadata", boom),
        caplog.at_level(logging.DEBUG, logger="worker.jobs"),
    ):
        await jobs.QuotientSync().run_pass()

    assert [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]


@pytest.mark.asyncio
async def test_the_loop_waits_the_backed_off_interval_after_a_failure(monkeypatch):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise TimeoutError

    monkeypatch.setattr(jobs, "sync_quotient_metadata", Mock(side_effect=[None, ValueError("Quotient down")]))
    monkeypatch.setattr(jobs.asyncio, "sleep", sleep)

    with pytest.raises(TimeoutError):
        await jobs.quotient_sync_loop()

    assert SYNC_MINUTES * 60 - 1 < sleeps[0] <= SYNC_MINUTES * 60
