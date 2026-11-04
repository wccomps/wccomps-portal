"""Tests for Nextcloud user_oidc OCS API manager."""

import inspect
import re
from unittest.mock import Mock

import httpx
import pytest
from django.test import override_settings

from core.nextcloud import NextcloudAPIError, NextcloudManager, nextcloud_configured, reason_for
from team.models import MAX_TEAMS


def _answer(body: object, status: int = 200, url: str = "https://files.test/x") -> Mock:
    response = Mock(is_success=200 <= status < 300, status_code=status, text="err", url=url)
    response.json.return_value = body
    return response


@pytest.fixture
def manager() -> NextcloudManager:
    client = Mock()
    return NextcloudManager(
        base_url="https://files.test/",
        username="admin",
        password="secret",
        provider_id=1,
        client=client,
    )


def test_nextcloud_configured_requires_url_username_and_password() -> None:
    with override_settings(NEXTCLOUD_URL="", NEXTCLOUD_USERNAME="admin", NEXTCLOUD_PASSWORD="pw"):
        assert not nextcloud_configured()
    with override_settings(NEXTCLOUD_URL="https://files.test", NEXTCLOUD_USERNAME="", NEXTCLOUD_PASSWORD="pw"):
        assert not nextcloud_configured()
    with override_settings(NEXTCLOUD_URL="https://files.test", NEXTCLOUD_USERNAME="admin", NEXTCLOUD_PASSWORD=""):
        assert not nextcloud_configured()
    with override_settings(NEXTCLOUD_URL="https://files.test", NEXTCLOUD_USERNAME="admin", NEXTCLOUD_PASSWORD="pw"):
        assert nextcloud_configured()


def test_provision_team_user_posts_oidc_user(manager: NextcloudManager) -> None:
    manager.client.post.return_value = _answer({"ocs": {"data": {"user_id": "team01"}}})

    manager.provision_team_user(1)

    manager.client.post.assert_called_once_with(
        "https://files.test/ocs/v2.php/apps/user_oidc/api/v1/user",
        json={"providerId": 1, "userId": "team01", "displayName": "Blue Team 01"},
    )


def test_delete_team_user_returns_true_on_200_and_false_on_404(manager: NextcloudManager) -> None:
    manager.client.delete.side_effect = [
        _answer({"ocs": {"data": {"user_id": "team07"}}}, 200),
        _answer({"ocs": {"data": {"message": "User not found"}}}, 404),
    ]

    assert manager.delete_team_user(7) is True
    assert manager.delete_team_user(7) is False
    assert manager.client.delete.call_args.args[0] == (
        "https://files.test/ocs/v2.php/apps/user_oidc/api/v1/user/team07"
    )


@pytest.mark.parametrize("number", [0, -1, MAX_TEAMS + 1])
def test_refuses_team_numbers_out_of_range(manager: NextcloudManager, number: int) -> None:
    with pytest.raises(NextcloudAPIError, match=f"Team number must be between 1 and {MAX_TEAMS}"):
        manager.provision_team_user(number)
    with pytest.raises(NextcloudAPIError, match=f"Team number must be between 1 and {MAX_TEAMS}"):
        manager.delete_team_user(number)
    manager.client.post.assert_not_called()
    manager.client.delete.assert_not_called()


def test_refuses_mismatched_user_id_or_redirect(manager: NextcloudManager) -> None:
    manager.client.post.return_value = _answer({"ocs": {"data": {"user_id": "admin"}}}, 200)
    with pytest.raises(NextcloudAPIError, match="different user"):
        manager.provision_team_user(1)

    manager.client.delete.return_value = _answer({"ocs": {"data": {"user_id": "team01"}}}, 302)
    with pytest.raises(NextcloudAPIError, match="HTTP 302"):
        manager.delete_team_user(1)


def test_network_and_http_errors_keep_url_and_body_out_of_reason_for(manager: NextcloudManager) -> None:
    manager.client.post.return_value = _answer({}, 401, url="https://files.internal/secret")
    with pytest.raises(NextcloudAPIError) as exc_info:
        manager.provision_team_user(1)
    assert reason_for(exc_info.value) == (
        "Provision Nextcloud user team01: Authentication failed (check NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD)"
    )
    assert "files.internal" in str(exc_info.value)

    manager.client.post.side_effect = httpx.ConnectError("Connection refused")
    with pytest.raises(NextcloudAPIError, match=r"Network error \(ConnectError\)"):
        manager.provision_team_user(1)


def test_unconfigured_manager_refuses_requests() -> None:
    with (
        override_settings(NEXTCLOUD_URL="", NEXTCLOUD_USERNAME="", NEXTCLOUD_PASSWORD=""),
        NextcloudManager() as unconfigured,
        pytest.raises(NextcloudAPIError, match="credentials not configured"),
    ):
        unconfigured.provision_team_user(1)


def test_only_request_sends_to_nextcloud() -> None:
    senders = {
        name
        for name, member in vars(NextcloudManager).items()
        if callable(member) and re.search(r"self\.client\b", inspect.getsource(member))
    }

    assert senders == {"__init__", "_request", "close"}
