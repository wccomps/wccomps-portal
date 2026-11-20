"""Run a worker loop (worker.jobs) through one pass."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from worker import jobs


async def one_pass(loop: Callable[[], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run loop until it sleeps after its first pass. asyncio.sleep stays patched for the rest of the test."""

    async def stop(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(jobs.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await loop()
