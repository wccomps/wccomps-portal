"""Tests for Authentik API manager."""

from unittest.mock import Mock, patch

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.authentik_manager import SESSIONS_HIDDEN, AuthentikAPIError, AuthentikManager


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
        assert "the token lacks required permissions" in str(error)

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

    @staticmethod
    def _answer(body: dict, status: int = 200, url: str = "https://auth.test.local/x") -> Mock:
        response = Mock(is_success=200 <= status < 300, status_code=status, text="", url=url)
        response.json.return_value = body
        if status >= 400:
            response.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=Mock(), response=response)
        return response

    def _sessions(self, *owners: int) -> Mock:
        return self._answer(
            {"results": [{"uuid": f"00000000-0000-4000-8000-{i:012d}", "user": pk} for i, pk in enumerate(owners)]}
        )

    TEAM01 = {"pk": 42, "username": "team01"}

    def test_end_user_sessions_ends_each_of_the_users_sessions(self, manager: AuthentikManager) -> None:
        manager.client.get.return_value = self._sessions(42, 42)
        manager.client.delete.return_value = self._answer({}, 204)

        assert manager.end_user_sessions(self.TEAM01) is None

        assert manager.client.delete.call_count == 2

    def test_end_user_sessions_filters_by_username(self, manager: AuthentikManager) -> None:
        manager.client.get.return_value = self._sessions(42)
        manager.client.delete.return_value = self._answer({}, 204)

        manager.end_user_sessions(self.TEAM01)

        assert manager.client.get.call_args.kwargs["params"]["user__username"] == "team01"

    def test_end_user_sessions_refuses_other_users_sessions(self, manager: AuthentikManager) -> None:
        manager.client.get.return_value = self._sessions(42, 99)

        error = manager.end_user_sessions(self.TEAM01)

        assert error is not None and "other users" in error
        manager.client.delete.assert_not_called()

    def test_end_user_sessions_no_sessions(self, manager: AuthentikManager) -> None:
        """No sessions of the user, while the token sees someone's: nothing to end."""
        manager.client.get.side_effect = [self._sessions(), self._sessions(7)]

        assert manager.end_user_sessions(self.TEAM01) is None

    def test_end_user_sessions_fails_when_sessions_are_hidden(self, manager: AuthentikManager) -> None:
        """No sessions found, and the token sees no one's: it may not see sessions at all, so nothing was ended."""
        manager.client.get.side_effect = [self._sessions(), self._sessions()]

        assert manager.end_user_sessions(self.TEAM01) == SESSIONS_HIDDEN
        manager.client.delete.assert_not_called()

    def test_end_user_sessions_already_ended_session_is_fine(self, manager: AuthentikManager) -> None:
        """A session that expired between the list and the delete answers 404."""
        manager.client.get.return_value = self._sessions(42, 42)
        manager.client.delete.side_effect = [self._answer({}, 404), self._answer({}, 204)]

        assert manager.end_user_sessions(self.TEAM01) is None

    def test_end_user_sessions_failed_delete_is_reported(self, manager: AuthentikManager) -> None:
        """A token that may view but not delete sessions must not report them ended."""
        manager.client.get.return_value = self._sessions(42, 42)
        manager.client.delete.side_effect = [self._answer({}, 204), self._answer({"detail": "denied"}, 403)]

        assert manager.end_user_sessions(self.TEAM01) == "Authentik did not end sessions (HTTP 403)"

    def test_end_user_sessions_refused_list_names_the_cause(self, manager: AuthentikManager) -> None:
        manager.client.get.return_value = self._answer({"detail": "denied"}, 403)

        error = manager.end_user_sessions(self.TEAM01)

        assert error is not None and "Permission denied" in error

    def test_end_user_sessions_reasons_never_name_the_user(self, manager: AuthentikManager) -> None:
        """One reason shared by many teams must read once in a bulk reset, so it can't carry the username."""
        manager.client.get.return_value = self._answer(
            {"detail": "denied"}, 403, url="https://a/?user__username=team01"
        )
        refused_list = manager.end_user_sessions(self.TEAM01)
        manager.client.get.return_value = self._sessions(42)
        manager.client.delete.side_effect = [self._answer({"detail": "denied"}, 403, url="https://a/s/0000/")]
        refused_delete = manager.end_user_sessions(self.TEAM01)
        manager.client.get.side_effect = httpx.ConnectError("https://a/?user__username=team01")
        network = manager.end_user_sessions(self.TEAM01)

        for reason in (refused_list, refused_delete, network):
            assert reason is not None and "team01" not in reason and "0000" not in reason

    def test_end_user_sessions_network_error(self, manager: AuthentikManager) -> None:
        manager.client.get.side_effect = httpx.ConnectError("Network error")

        assert manager.end_user_sessions(self.TEAM01) == "Network error (ConnectError)"

    def test_sees_any_sessions(self, manager: AuthentikManager) -> None:
        manager.client.get.side_effect = [self._sessions(7), self._sessions()]

        assert manager.sees_any_sessions() is True
        assert manager.sees_any_sessions() is False
        assert manager.client.get.call_args.kwargs["params"] == {"page_size": "1"}

    def test_end_user_sessions_refuses_a_session_id_that_isnt_a_uuid(self, manager: AuthentikManager) -> None:
        """A session id goes into the DELETE path, so one like ../users/5 must never be sent."""
        manager.client.get.return_value = self._answer({"results": [{"uuid": "../users/5", "user": 42}]})

        error = manager.end_user_sessions(self.TEAM01)

        assert error is not None and "valid id" in error
        manager.client.delete.assert_not_called()

    def test_end_user_sessions_redirected_delete_is_not_ended(self, manager: AuthentikManager) -> None:
        """A redirect isn't an error status, but the session wasn't ended either."""
        manager.client.get.return_value = self._sessions(42)
        manager.client.delete.return_value = self._answer({}, 302)

        assert manager.end_user_sessions(self.TEAM01) == "Authentik did not end sessions (HTTP 302)"

    def test_set_password_redirect_is_a_failure(self, manager: AuthentikManager) -> None:
        """A redirected POST never set the password, so it must not be handed out."""
        manager.client.post.return_value = self._answer({}, 302)

        with pytest.raises(AuthentikAPIError):
            manager.set_password(self.TEAM01, "New-Pass-1!")

    def test_end_user_sessions_redirected_list_is_not_taken_as_no_sessions(self, manager: AuthentikManager) -> None:
        """A redirect carrying an empty result list must not read as "this user has no sessions"."""
        manager.client.get.side_effect = [self._answer({"results": []}, 302), self._sessions(7)]

        assert manager.end_user_sessions(self.TEAM01) is not None
        manager.client.delete.assert_not_called()

    def test_end_user_sessions_deletes_by_the_canonical_session_id(self, manager: AuthentikManager) -> None:
        """uuid.UUID accepts {...} and urn:uuid: forms; the delete path must be the plain form."""
        manager.client.get.return_value = self._answer(
            {"results": [{"uuid": "{00000000-0000-4000-8000-000000000001}", "user": 42}]}
        )
        manager.client.delete.return_value = self._answer({}, 204)

        assert manager.end_user_sessions(self.TEAM01) is None

        assert manager.client.delete.call_args.args[0].endswith(
            "/authenticated_sessions/00000000-0000-4000-8000-000000000001/"
        )
