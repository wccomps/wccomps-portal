"""Stored Authentik groups are refreshed by the bot, not only at web login (security review, Medium)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bot.cogs import authentik_groups
from core.authentik_manager import AuthentikAPIError, AuthentikManager
from core.services.user_groups import GroupRefreshAbortedError


async def _run_with(result=None, error=None):
    with patch.object(authentik_groups, "refresh_user_groups_unless_running", side_effect=error, return_value=result):
        await authentik_groups.refresh_groups_now()


@pytest.mark.asyncio
async def test_the_refresh_runs_off_the_shared_orm_thread():
    """Waiting on the refresh lock must not hold the thread every other ORM call in the bot waits on."""
    off_thread = AsyncMock(return_value=MagicMock(changed=0))
    with patch.dict(authentik_groups.refresh_groups_now.__globals__, {"off_orm_thread": off_thread}):
        await authentik_groups.refresh_groups_now()

    off_thread.assert_awaited_once_with(authentik_groups._refresh)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [GroupRefreshAbortedError("no users"), AuthentikAPIError("down"), RuntimeError("x")])
async def test_failures_are_logged_not_raised(error):
    await _run_with(error=error)


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
                "results": [{"uid": f"u{page}", "is_active": True, "groups": []}],
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
