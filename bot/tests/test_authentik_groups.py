"""Stored Authentik groups are refreshed by the bot, not only at web login (security review, Medium)."""

from unittest.mock import patch

import httpx
import pytest

from bot.cogs import authentik_groups
from core.authentik_manager import AuthentikAPIError, AuthentikManager
from core.services.user_groups import GroupRefreshAbortedError


async def _run_with(result=None, error=None):
    with patch.object(authentik_groups, "refresh_user_groups", side_effect=error, return_value=result):
        await authentik_groups.refresh_groups_now()


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
