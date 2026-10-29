"""The bot recovers from a dead database connection without a restart.

The bot serves no HTTP requests, so Django's close_old_connections (wired to request_started /
request_finished) never ran for it: after a Postgres restart its one connection stayed dead and
every loop logged errors forever while the process looked healthy.
"""

import pytest
import pytest_asyncio
from asgiref.sync import sync_to_async
from django.db import connection
from django.db.utils import Error as DjangoDBError

from bot.utils import recycle_db_connection
from core.models import DiscordTask

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest_asyncio.fixture(autouse=True)
async def _fresh_connection_each_test():
    """Tests share the bot's single ORM thread; never hand the next test a connection we killed."""
    await sync_to_async(lambda: connection.close())()
    yield
    await sync_to_async(lambda: connection.close())()


@sync_to_async
def _count_tasks() -> int:
    return DiscordTask.objects.count()


@sync_to_async
def _kill_connection_under_django() -> None:
    """Simulate a Postgres restart/failover: the socket dies, Django still holds the object."""
    connection.connection.close()


async def _dead_connection() -> None:
    await _count_tasks()  # open the bot's connection on its ORM thread
    await _kill_connection_under_django()
    with pytest.raises(DjangoDBError):
        await _count_tasks()  # first failure marks the connection as having errors


async def test_dead_connection_stays_dead_without_recycling():
    """Documents the bug: nothing reconnects on its own."""
    await _dead_connection()

    with pytest.raises(DjangoDBError):
        await _count_tasks()


async def test_recycling_reconnects_after_connection_dies():
    await _dead_connection()

    await recycle_db_connection()

    assert await _count_tasks() == 0


async def test_recycling_is_harmless_on_healthy_connection():
    await _count_tasks()

    await recycle_db_connection()

    assert await _count_tasks() == 0


@pytest.mark.parametrize(
    ("module", "loop_attr", "work_attr"),
    [
        ("bot.discord_queue", "DiscordQueueProcessor", "_process_pending_tasks"),
        ("bot.unified_dashboard", "UnifiedDashboard", "_pass"),
    ],
)
async def test_each_loop_recycles_before_its_work(module, loop_attr, work_attr, monkeypatch):
    """Each polling loop must recycle the connection at the start of every pass."""
    import importlib
    from unittest.mock import AsyncMock, MagicMock

    mod = importlib.import_module(module)
    cls = getattr(mod, loop_attr)
    obj = cls.__new__(cls)
    obj.bot = MagicMock()
    obj.bot.wait_until_ready = AsyncMock()
    calls: list[str] = []

    async def fake_recycle() -> None:
        calls.append("recycle")

    async def fake_work(*_args: object) -> None:
        calls.append("work")
        obj.running = False  # one pass only

    monkeypatch.setattr(mod, "recycle_db_connection", fake_recycle)
    monkeypatch.setattr(obj, work_attr, fake_work)
    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
    obj.running = True

    loop_method = next(getattr(obj, n) for n in ("_process_loop", "_dashboard_loop") if hasattr(obj, n))
    await loop_method()

    assert calls == ["recycle", "work"]
