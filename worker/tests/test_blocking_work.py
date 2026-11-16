"""The worker's slow sync work (Authentik, Quotient) must not hold the one thread every other sync_to_async call
queues on, or the worker queue and the timer stall behind it."""

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from core.tests.shared_thread import shared_thread_answers_while
from worker import jobs


async def _group_refresh(blocked: Callable[..., Any]) -> None:
    with (
        patch.object(jobs, "AuthentikManager"),
        patch.object(jobs, "refresh_user_groups_unless_running", blocked),
    ):
        await jobs.group_refresh_pass()


async def _quotient_sync(blocked: Callable[..., Any]) -> None:
    with patch.object(jobs, "sync_quotient_metadata", blocked):
        await jobs.QuotientSync().run_pass()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("start", [_group_refresh, _quotient_sync], ids=["group-refresh", "quotient-sync"])
async def test_slow_sync_work_leaves_the_shared_thread_free(start, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path))
    assert await shared_thread_answers_while(start)
