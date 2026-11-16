"""Stored Authentik groups are refreshed by the worker, not only at web login (security review, Medium)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.authentik_manager import AuthentikAPIError, AuthentikManager
from core.models import QueuedTask
from core.queued_tasks import SyncRoles
from core.services.user_groups import GroupRefreshAbortedError, GroupRefreshResult
from worker import jobs


async def _run_with(result=None, error=None):
    with patch.object(jobs, "refresh_user_groups_unless_running", side_effect=error, return_value=result):
        await jobs.group_refresh_pass()


@pytest.mark.asyncio
async def test_the_refresh_runs_off_the_shared_orm_thread():
    """Waiting on the refresh lock must not hold the thread every other ORM call in the process waits on."""
    off_thread = AsyncMock(return_value=MagicMock(changed=0))
    with patch.dict(jobs.group_refresh_pass.__globals__, {"run_blocking": off_thread}):
        await jobs.group_refresh_pass()

    off_thread.assert_awaited_once_with(jobs.refresh_groups)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [GroupRefreshAbortedError("no users"), AuthentikAPIError("down"), RuntimeError("x")])
async def test_failures_are_logged_not_raised_and_still_beat(error, tmp_path, monkeypatch):
    """Authentik being down is no reason to restart the worker: the loop is alive and tries again."""
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path))
    with patch.object(jobs, "AuthentikManager"):
        await _run_with(error=error)
    assert (tmp_path / "groups").exists()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("changed", "queued"), [(2, [SyncRoles(requested_by="worker", dry_run=False)]), (0, [])])
async def test_group_refresh_queues_role_sync_only_on_change(monkeypatch, changed, queued):
    monkeypatch.setattr("worker.jobs.refresh_groups", lambda: GroupRefreshResult(checked=10, changed=changed))
    await jobs.group_refresh_pass()
    assert [t.typed_payload() async for t in QueuedTask.objects.all()] == queued


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_group_refresh_no_role_sync_without_discord(settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = ""
    monkeypatch.setattr("worker.jobs.refresh_groups", lambda: GroupRefreshResult(checked=10, changed=2))
    await jobs.group_refresh_pass()
    assert not await QueuedTask.objects.aexists()


@pytest.mark.asyncio
async def test_the_loop_refreshes_at_once_then_every_five_minutes(monkeypatch):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise TimeoutError

    monkeypatch.setattr(jobs, "group_refresh_pass", AsyncMock())
    monkeypatch.setattr(jobs.asyncio, "sleep", sleep)

    with pytest.raises(TimeoutError):
        await jobs.group_refresh_loop()

    jobs.group_refresh_pass.assert_awaited_once()
    assert 299 < sleeps[0] <= jobs.REFRESH_MINUTES * 60


def _manager(handler):
    return AuthentikManager(
        base_url="https://auth.test", api_token="t", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_list_all_users_follows_pagination():
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "results": [{"pk": page, "username": f"u{page}", "uid": f"u{page}", "is_active": True, "groups": []}],
                "pagination": {"next": page + 1 if page < 3 else 0},
            },
        )

    assert [u["uid"] for u in _manager(handler).list_all_users()] == ["u1", "u2", "u3"]


def test_list_all_raises_on_http_error():
    """A partial listing must never be mistaken for the full user list."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "2":
            return httpx.Response(503, json={"detail": "busy"})
        return httpx.Response(200, json={"results": [{"uid": "u1"}], "pagination": {"next": 2}})

    with pytest.raises(AuthentikAPIError):
        _manager(handler).list_all_groups()
