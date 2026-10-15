"""Tests for Authentik API manager."""

from unittest.mock import Mock, patch

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.authentik_manager import AuthentikAPIError, AuthentikManager


class TestAuthentikAPIError:
    """Test AuthentikAPIError exception class."""

    def test_error_with_all_fields(self) -> None:
        """Test error message formatting with all fields present."""
        error = AuthentikAPIError(
            message="Test error",
            status_code=404,
            response_text="Resource not found",
            url="https://auth.example.com/api/v3/test",
        )
        formatted = error.formatted_message()
        assert "Test error" in formatted
        assert "Status: 404" in formatted
        assert "Resource not found" in formatted
        assert "https://auth.example.com" in formatted

    def test_error_with_minimal_fields(self) -> None:
        """Test error message formatting with only required field."""
        error = AuthentikAPIError(message="Simple error")
        formatted = error.formatted_message()
        assert formatted == "Simple error"

    @given(
        message=st.text(min_size=1, max_size=100),
        status_code=st.integers(min_value=100, max_value=599),
    )
    def test_error_message_property(self, message: str, status_code: int) -> None:
        """Property: error message always contains the original message."""
        error = AuthentikAPIError(message=message, status_code=status_code)
        assert message in str(error)
        assert f"Status: {status_code}" in str(error)


@pytest.mark.django_db
class TestAuthentikManager:
    """Test AuthentikManager class."""

    @pytest.fixture
    def manager(self) -> AuthentikManager:
        """Create AuthentikManager instance with mocked client."""
        with patch("core.authentik_manager.settings") as mock_settings:
            mock_settings.AUTHENTIK_URL = "https://auth.test.local"
            mock_settings.AUTHENTIK_TOKEN = "test-token-123"
            mgr = AuthentikManager()
            mgr.client = Mock()
            return mgr

    def test_handle_response_error_401(self, manager: AuthentikManager) -> None:
        """Test error handling for 401 Unauthorized."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.url = "https://auth.test.local/api/v3/test"
        mock_response.text = "Unauthorized"
        mock_response.json.side_effect = ValueError("Not JSON")

        error = manager._handle_response_error(mock_response, "Test context")

        assert error.status_code == 401
        assert "Authentication failed" in str(error)
        assert "check AUTHENTIK_TOKEN" in str(error)

    def test_handle_response_error_403(self, manager: AuthentikManager) -> None:
        """Test error handling for 403 Forbidden."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.url = "https://auth.test.local/api/v3/test"
        mock_response.json.return_value = {"detail": "Permission denied"}

        error = manager._handle_response_error(mock_response, "Test operation")

        assert error.status_code == 403
        assert "Permission denied" in str(error)
        assert "token lacks required permissions" in str(error)

    def test_handle_response_error_404(self, manager: AuthentikManager) -> None:
        """Test error handling for 404 Not Found."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.url = "https://auth.test.local/api/v3/test"
        mock_response.text = "Not found"
        mock_response.json.side_effect = ValueError("Not JSON")

        error = manager._handle_response_error(mock_response, "Find resource")

        assert error.status_code == 404
        assert "Resource not found" in str(error)

    @given(status_code=st.integers(min_value=500, max_value=599))
    def test_handle_response_error_5xx(self, status_code: int) -> None:
        """Property: 5xx errors are handled consistently."""
        with patch("core.authentik_manager.settings") as mock_settings:
            mock_settings.AUTHENTIK_URL = "https://auth.test.local"
            mock_settings.AUTHENTIK_TOKEN = "test-token-123"
            manager = AuthentikManager()

            mock_response = Mock()
            mock_response.status_code = status_code
            mock_response.url = "https://auth.test.local/api/v3/test"
            mock_response.text = "Server error"
            mock_response.json.side_effect = ValueError("Not JSON")

            error = manager._handle_response_error(mock_response, "Server operation")

            assert error.status_code == status_code
            assert "Server operation" in str(error)

    def test_get_application_by_slug_success(self, manager: AuthentikManager) -> None:
        """Looks the app up directly by slug (the list endpoint hides apps; see #43)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"pk": "app-123", "slug": "netbird", "name": "NetBird VPN"}
        manager.client.get.return_value = mock_response

        app = manager.get_application_by_slug("netbird")

        assert app is not None
        assert app["pk"] == "app-123"
        manager.client.get.assert_called_once_with("https://auth.test.local/api/v3/core/applications/netbird/")

    def test_get_application_by_slug_not_found(self, manager: AuthentikManager) -> None:
        """A 404 from the retrieve endpoint means the slug doesn't exist."""
        mock_response = Mock()
        mock_response.status_code = 404
        manager.client.get.return_value = mock_response

        app = manager.get_application_by_slug("nonexistent")

        assert app is None
        mock_response.raise_for_status.assert_not_called()

    def test_get_application_by_slug_does_not_depend_on_list_endpoint(self, manager: AuthentikManager) -> None:
        """Authentik's list endpoint filters per user and can omit apps the account can access."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"pk": "app-9", "slug": "quotient2"}
        manager.client.get.return_value = mock_response

        manager.get_application_by_slug("quotient2")

        called_url = manager.client.get.call_args.args[0]
        assert called_url.endswith("/api/v3/core/applications/quotient2/")
        assert "params" not in manager.client.get.call_args.kwargs

    def test_get_application_by_slug_http_error(self, manager: AuthentikManager) -> None:
        """Test HTTP error handling during application retrieval."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.url = "https://auth.test.local/api/v3/core/applications/"
        mock_response.text = "Internal server error"
        mock_response.json.side_effect = ValueError("Not JSON")
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=Mock(), response=mock_response
        )
        manager.client.get.return_value = mock_response

        app = manager.get_application_by_slug("test-app")

        assert app is None
        manager.client.get.assert_called_once()

    def test_get_application_by_slug_network_error(self, manager: AuthentikManager) -> None:
        """Test network error handling during application retrieval."""
        manager.client.get.side_effect = httpx.ConnectError("Connection refused")

        app = manager.get_application_by_slug("test-app")

        assert app is None

    def test_get_blueteam_binding_success(self, manager: AuthentikManager) -> None:
        """Test successful BlueTeam binding retrieval."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "results": [
                {
                    "pk": "binding-123",
                    "group": "group-456",
                    "group_obj": {"name": "WCComps_BlueTeam", "pk": "group-456"},
                    "enabled": True,
                }
            ]
        }
        manager.client.get.return_value = mock_response

        binding, error = manager.get_blueteam_binding("app-123")

        assert binding is not None
        assert error is None
        assert binding["pk"] == "binding-123"
        assert "blueteam" in binding["group_obj"]["name"].lower()

    def test_get_blueteam_binding_not_found(self, manager: AuthentikManager) -> None:
        """Test BlueTeam binding not found."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "results": [
                {
                    "pk": "binding-123",
                    "group": "group-789",
                    "group_obj": {"name": "WCComps_GoldTeam", "pk": "group-789"},
                }
            ]
        }
        manager.client.get.return_value = mock_response

        binding, error = manager.get_blueteam_binding("app-123")

        assert binding is None
        assert error is not None
        assert "No BlueTeam group binding found" in error

    def test_get_blueteam_binding_empty_results(self, manager: AuthentikManager) -> None:
        """Test BlueTeam binding with no results."""
        mock_response = Mock()
        mock_response.json.return_value = {"results": []}
        manager.client.get.return_value = mock_response

        binding, error = manager.get_blueteam_binding("app-123")

        assert binding is None
        assert error is not None
        assert "No BlueTeam group binding found" in error

    def test_update_binding_enabled_success(self, manager: AuthentikManager) -> None:
        """Test successfully updating binding enabled state."""
        binding = {
            "pk": "binding-123",
            "group": "group-456",
            "enabled": False,
        }

        mock_response = Mock()
        manager.client.put.return_value = mock_response

        result = manager.update_binding_enabled(binding, True)

        assert result is True
        manager.client.put.assert_called_once()
        call_args = manager.client.put.call_args
        assert call_args[1]["json"]["enabled"] is True

    def test_update_binding_enabled_failure(self, manager: AuthentikManager) -> None:
        """Test binding update failure handling."""
        binding = {"pk": "binding-123", "enabled": False}
        manager.client.put.side_effect = httpx.ConnectError("Network error")

        result = manager.update_binding_enabled(binding, True)

        assert result is False

    def test_enable_application_success(self, manager: AuthentikManager) -> None:
        """Test successfully enabling an application."""
        with (
            patch.object(manager, "get_application_by_slug") as mock_get_app,
            patch.object(manager, "get_blueteam_binding") as mock_get_binding,
            patch.object(manager, "update_binding_enabled") as mock_update,
        ):
            mock_get_app.return_value = {"pk": "app-123", "slug": "netbird"}
            mock_get_binding.return_value = (
                {"pk": "binding-456", "enabled": False},
                None,
            )
            mock_update.return_value = True

            success, error = manager.enable_application("netbird")

            assert success is True
            assert error is None
            # Check that update_binding_enabled was called with enabled=True
            # (enabling the binding allows blue team access)
            mock_update.assert_called_once()
            call_args = mock_update.call_args
            assert call_args[0][0] == {"pk": "binding-456", "enabled": False}
            assert call_args[1]["enabled"] is True

    def test_enable_application_not_found(self, manager: AuthentikManager) -> None:
        """Test enabling non-existent application."""
        with patch.object(manager, "get_application_by_slug") as mock_get_app:
            mock_get_app.return_value = None

            success, error = manager.enable_application("nonexistent")

            assert success is False
            assert error is not None
            assert "not found" in error

    def test_enable_application_no_binding(self, manager: AuthentikManager) -> None:
        """Test enabling application with no BlueTeam binding."""
        with (
            patch.object(manager, "get_application_by_slug") as mock_get_app,
            patch.object(manager, "get_blueteam_binding") as mock_get_binding,
        ):
            mock_get_app.return_value = {"pk": "app-123"}
            mock_get_binding.return_value = (None, "No binding found")

            success, error = manager.enable_application("test-app")

            assert success is False
            assert error is not None
            assert "No binding found" in error

    def test_disable_application_success(self, manager: AuthentikManager) -> None:
        """Test successfully disabling an application."""
        with (
            patch.object(manager, "get_application_by_slug") as mock_get_app,
            patch.object(manager, "get_blueteam_binding") as mock_get_binding,
            patch.object(manager, "update_binding_enabled") as mock_update,
        ):
            mock_get_app.return_value = {"pk": "app-123"}
            mock_get_binding.return_value = (
                {"pk": "binding-456", "enabled": True},
                None,
            )
            mock_update.return_value = True

            success, error = manager.disable_application("netbird")

            assert success is True
            assert error is None
            # Check that update_binding_enabled was called with enabled=False
            # (disabling the binding blocks blue team access)
            mock_update.assert_called_once()
            call_args = mock_update.call_args
            assert call_args[0][0] == {"pk": "binding-456", "enabled": True}
            assert call_args[1]["enabled"] is False

    def test_update_user_discord_id_success(self, manager: AuthentikManager) -> None:
        """Test successfully updating user's Discord ID."""
        mock_get_response = Mock()
        mock_get_response.json.return_value = {
            "results": [{"pk": 42, "uid": "u-1", "attributes": {"existing_key": "value"}}],
        }

        mock_patch_response = Mock()

        manager.client.get.return_value = mock_get_response
        manager.client.patch.return_value = mock_patch_response

        result = manager.update_user_discord_id("testuser", 123456789, "u-1")

        assert result is True
        manager.client.patch.assert_called_once()
        call_args = manager.client.patch.call_args
        assert "/42/" in call_args[0][0]
        assert call_args[1]["json"]["attributes"]["discord_id"] == "123456789"
        # Verify existing attributes are preserved
        assert call_args[1]["json"]["attributes"]["existing_key"] == "value"

    def test_update_user_discord_id_failure(self, manager: AuthentikManager) -> None:
        """Test handling failure when updating Discord ID."""
        manager.client.get.side_effect = httpx.ConnectError("Network error")

        result = manager.update_user_discord_id("testuser", 123456789, "u-1")

        assert result is False

    def test_revoke_user_sessions_success(self, manager: AuthentikManager) -> None:
        """Test successfully revoking user sessions."""
        mock_user_response = Mock()
        mock_user_response.json.return_value = {"results": [{"pk": 42}]}

        mock_sessions_response = Mock()
        mock_sessions_response.json.return_value = {
            "results": [
                {"uuid": "session-1"},
                {"uuid": "session-2"},
            ]
        }

        mock_delete_response = Mock()

        manager.client.get.side_effect = [mock_user_response, mock_sessions_response]
        manager.client.delete.return_value = mock_delete_response

        success, error, count = manager.revoke_user_sessions("team01")

        assert success is True
        assert error is None
        assert count == 2
        assert manager.client.delete.call_count == 2

    def test_revoke_user_sessions_user_not_found(self, manager: AuthentikManager) -> None:
        """Test revoking sessions for non-existent user."""
        mock_response = Mock()
        mock_response.json.return_value = {"results": []}
        manager.client.get.return_value = mock_response

        success, error, count = manager.revoke_user_sessions("nonexistent")

        assert success is False
        assert "not found" in error
        assert count == 0

    def test_revoke_user_sessions_no_sessions(self, manager: AuthentikManager) -> None:
        """Test revoking sessions when user has none."""
        mock_user_response = Mock()
        mock_user_response.json.return_value = {"results": [{"pk": 42}]}

        mock_sessions_response = Mock()
        mock_sessions_response.json.return_value = {"results": []}

        manager.client.get.side_effect = [mock_user_response, mock_sessions_response]

        success, error, count = manager.revoke_user_sessions("team01")

        assert success is True
        assert error is None
        assert count == 0

    def test_revoke_user_sessions_network_error(self, manager: AuthentikManager) -> None:
        """Test handling network error during session revocation."""
        manager.client.get.side_effect = httpx.ConnectError("Network error")

        success, error, count = manager.revoke_user_sessions("team01")

        assert success is False
        assert "Network error" in error
        assert count == 0

    def test_revoke_user_sessions_partial_failure(self, manager: AuthentikManager) -> None:
        """Test partial failure when revoking some sessions."""
        mock_user_response = Mock()
        mock_user_response.json.return_value = {"results": [{"pk": 42}]}

        mock_sessions_response = Mock()
        mock_sessions_response.json.return_value = {
            "results": [
                {"uuid": "session-1"},
                {"uuid": "session-2"},
                {"uuid": "session-3"},
            ]
        }

        mock_delete_success = Mock()
        manager.client.get.side_effect = [mock_user_response, mock_sessions_response]
        manager.client.delete.side_effect = [mock_delete_success, Exception("Delete failed"), mock_delete_success]

        success, error, count = manager.revoke_user_sessions("team01")

        assert success is True
        assert error is None
        assert count == 2  # 2 succeeded, 1 failed
