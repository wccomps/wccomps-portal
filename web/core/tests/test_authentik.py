"""Tests for AuthentikManager update_user_discord_id method."""

import logging
from unittest.mock import MagicMock, Mock, patch

import httpx
import pytest

from core.authentik_manager import AuthentikManager


class TestAuthentikManager:
    """Tests for AuthentikManager class."""

    @pytest.fixture
    def manager(self):
        """Create AuthentikManager with mocked settings."""
        with patch("core.authentik_manager.settings") as mock_settings:
            mock_settings.AUTHENTIK_URL = "https://auth.example.com"
            mock_settings.AUTHENTIK_TOKEN = "test-token"
            return AuthentikManager()

    def test_init_sets_base_url_and_token(self, manager):
        """Manager should initialize with base URL and token from settings."""
        assert manager.base_url == "https://auth.example.com"
        assert manager.token == "test-token"

    def test_client_headers_contain_bearer_token(self, manager):
        """Client headers should contain Bearer token."""
        assert manager.client.headers["Authorization"] == "Bearer test-token"
        assert manager.client.headers["Content-Type"] == "application/json"


class TestUpdateUserDiscordId:
    """Tests for update_user_discord_id method."""

    @pytest.fixture
    def manager(self):
        """Create AuthentikManager with mocked client."""
        with patch("core.authentik_manager.settings") as mock_settings:
            mock_settings.AUTHENTIK_URL = "https://auth.example.com"
            mock_settings.AUTHENTIK_TOKEN = "test-token"
            mgr = AuthentikManager()
            mgr.client = Mock()
            return mgr

    def test_updates_user_with_discord_id(self, manager):
        """Should look up user by username, then PATCH with discord_id."""
        # Mock GET response (user search by username)
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {
            "results": [
                {
                    "pk": 42,
                    "uid": "u-1",
                    "attributes": {"existing_attr": "value"},
                }
            ],
        }
        manager.client.get.return_value = mock_get_response

        # Mock PATCH response
        mock_patch_response = MagicMock()
        manager.client.patch.return_value = mock_patch_response

        result = manager.update_user_discord_id("testuser", 123456789, "u-1")

        assert result is True

        # Verify GET searched by username
        manager.client.get.assert_called_once()
        assert "username=testuser" in manager.client.get.call_args[0][0]

        # Verify PATCH used the integer PK
        manager.client.patch.assert_called_once()
        assert "/42/" in manager.client.patch.call_args[0][0]
        call_kwargs = manager.client.patch.call_args[1]
        assert call_kwargs["json"]["attributes"]["discord_id"] == "123456789"
        assert call_kwargs["json"]["attributes"]["existing_attr"] == "value"

    def test_preserves_existing_attributes(self, manager):
        """Should preserve existing user attributes when adding discord_id."""
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {
            "results": [
                {
                    "pk": 7,
                    "uid": "u-1",
                    "attributes": {"role": "admin", "team": "gold"},
                }
            ],
        }
        manager.client.get.return_value = mock_get_response
        manager.client.patch.return_value = MagicMock()

        manager.update_user_discord_id("golduser", 999999999, "u-1")

        call_kwargs = manager.client.patch.call_args[1]
        attrs = call_kwargs["json"]["attributes"]
        assert attrs["role"] == "admin"
        assert attrs["team"] == "gold"
        assert attrs["discord_id"] == "999999999"

    def test_refuses_to_write_to_a_different_identity_with_that_username(self, manager):
        """A stale portal username may now belong to someone else in Authentik: check the uid first."""
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {"results": [{"pk": 9, "uid": "someone-else", "attributes": {}}]}
        manager.client.get.return_value = mock_get_response

        result = manager.update_user_discord_id("reused", 123456789, "my-uid")

        assert result is False
        manager.client.patch.assert_not_called()

    def test_returns_false_when_user_not_found(self, manager):
        """Should return False if username lookup returns no results."""
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {"results": []}
        manager.client.get.return_value = mock_get_response

        result = manager.update_user_discord_id("nonexistent", 123456789, "u-1")

        assert result is False
        manager.client.patch.assert_not_called()

    def test_raises_on_403_error(self, manager):
        """Should raise on 403 Forbidden (permission issue)."""
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Permission denied"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock_response
        )
        manager.client.get.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            manager.update_user_discord_id("someuser", 123456789, "u-1")

    def test_raises_on_other_http_errors(self, manager):
        """Should raise on other HTTP errors."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Server error"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock_response
        )
        manager.client.get.return_value = mock_response

        with pytest.raises(httpx.HTTPStatusError):
            manager.update_user_discord_id("someuser", 123456789, "u-1")


class TestUpdateBindingEnabled:
    """update_binding_enabled logs Authentik's response body so a 400 is diagnosable."""

    @pytest.fixture
    def manager(self):
        with patch("core.authentik_manager.settings") as mock_settings:
            mock_settings.AUTHENTIK_URL = "https://auth.example.com"
            mock_settings.AUTHENTIK_TOKEN = "test-token"
            mgr = AuthentikManager()
            mgr.client = Mock()
            return mgr

    def test_logs_response_body_on_http_error(self, manager, caplog):
        resp = MagicMock()
        resp.status_code = 400
        resp.url = "https://auth.example.com/api/v3/policies/bindings/abc/"
        resp.json.return_value = {"non_field_errors": ["Enabled cannot be set on this binding"]}
        resp.text = '{"non_field_errors": ["Enabled cannot be set on this binding"]}'
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("400", request=MagicMock(), response=resp)
        manager.client.put.return_value = resp

        with caplog.at_level(logging.ERROR):
            result = manager.update_binding_enabled({"pk": "abc", "enabled": False}, True)

        assert result is False
        assert "Enabled cannot be set on this binding" in caplog.text
