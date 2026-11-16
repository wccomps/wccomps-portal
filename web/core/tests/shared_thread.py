"""Whether slow sync work leaves free the one thread every other sync_to_async call queues on, for the chat and worker
processes' tests alike."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

from asgiref.sync import sync_to_async


async def shared_thread_answers_while(start: Callable[[Callable[..., Any]], Awaitable[object]]) -> bool:
    """Whether a plain sync_to_async call completes while the work `start` runs is blocked in its sync part."""
    entered, release = threading.Event(), threading.Event()

    def blocked(*_args: object, **_kwargs: object) -> Any:
        entered.set()
        release.wait(10)
        return MagicMock()

    work = asyncio.ensure_future(start(blocked))
    try:
        await asyncio.to_thread(entered.wait, 5)
        assert entered.is_set(), "the work never reached its sync part"
        try:
            await asyncio.wait_for(sync_to_async(lambda: None)(), timeout=1)
        except TimeoutError:
            return False
        return True
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)
