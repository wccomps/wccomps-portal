"""Blocking work runs off the ORM thread every other bot call waits on."""

from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import sync_to_async

from bot import utils

pytestmark = pytest.mark.asyncio


async def test_the_helper_runs_on_a_pool_thread_and_closes_its_connection():
    flags: list[bool] = []

    def recording(func, *, thread_sensitive=True):
        flags.append(thread_sensitive)
        return sync_to_async(func, thread_sensitive=thread_sensitive)

    connection = MagicMock()
    with patch.dict(utils.off_orm_thread.__globals__, {"sync_to_async": recording, "connection": connection}):
        assert await utils.off_orm_thread(lambda a, b=0: a + b, 2, b=3) == 5

    assert flags == [False]
    connection.close.assert_called_once()
