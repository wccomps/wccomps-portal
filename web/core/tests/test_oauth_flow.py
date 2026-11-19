"""
Tests for OAuth flow with mocked Authentik responses.

These tests verify the full OAuth callback logic without requiring
a real Authentik server.
"""

import re
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.contrib.auth.models import User
from django.core import signing
from django.test import Client
from django.utils import timezone

from core.models import CredentialsReset, UserGroups

OAUTH_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-secret",
    "authorization_endpoint": "https://auth.example.com/authorize/",
    "token_endpoint": "https://auth.example.com/token/",
    "userinfo_endpoint": "https://auth.example.com/userinfo/",
    "end_session_endpoint": "https://auth.example.com/end-session/",
}


@pytest.fixture
def oauth_state_session(client: Client) -> tuple[Client, str]:
    """Set up a client with valid OAuth state from a login redirect."""
    from urllib.parse import parse_qs, urlparse

    with patch("core.oauth._get_oauth_config") as mock_config:
        mock_config.return_value = {
            "client_id": "test-client-id",
            "client_secret": "test-secret",
            "authorization_endpoint": "https://auth.example.com/authorize/",
            "token_endpoint": "https://auth.example.com/token/",
            "userinfo_endpoint": "https://auth.example.com/userinfo/",
            "end_session_endpoint": "https://auth.example.com/end-session/",
        }
        response = client.get("/auth/login/")
        assert response.status_code == 302

    # Extract the signed state from the redirect URL
    parsed = urlparse(response.url)
    state = parse_qs(parsed.query)["state"][0]
    return client, state


@pytest.mark.django_db
class TestOAuthLogin:
    """Test OAuth login initiation."""

    def test_login_redirects_to_authentik(self):
        """Login should redirect to Authentik authorization endpoint."""
        client = Client()
        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            response = client.get("/auth/login/")

        assert response.status_code == 302
        assert "auth.example.com/authorize" in response.url
        assert "client_id=test-client-id" in response.url
        assert "scope=openid" in response.url

    def test_login_includes_signed_state_in_redirect(self):
        """Login should include a signed state parameter in the redirect URL."""
        from urllib.parse import parse_qs, urlparse

        from django.core import signing

        client = Client()
        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            response = client.get("/auth/login/")

        parsed = urlparse(response.url)
        state = parse_qs(parsed.query)["state"][0]
        state_data = signing.loads(state, max_age=300)
        assert "n" in state_data  # Contains nonce
        assert len(state_data["n"]) > 10  # Secure random nonce

    def test_login_encodes_next_url_in_state(self):
        """Login should encode next URL in the signed state parameter."""
        from urllib.parse import parse_qs, urlparse

        from django.core import signing

        client = Client()
        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            response = client.get("/auth/login/?next=/dashboard/")

        parsed = urlparse(response.url)
        state = parse_qs(parsed.query)["state"][0]
        state_data = signing.loads(state, max_age=300)
        assert state_data["next"] == "/dashboard/"

    def test_login_without_client_id_shows_error(self):
        """Login without client ID configured should show error."""
        client = Client()
        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "",
                "client_secret": "",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            response = client.get("/auth/login/")

        assert response.status_code == 500
        assert b"Configuration Error" in response.content

    def test_login_without_oidc_url_shows_error(self, settings):
        """AUTHENTIK_OIDC_URL defaults to empty without AUTHENTIK_URL: no redirect to a relative /authorize/."""
        settings.AUTHENTIK_CLIENT_ID = "test-client-id"
        settings.AUTHENTIK_OIDC_URL = ""

        response = Client().get("/auth/login/")

        assert response.status_code == 500
        assert b"Configuration Error" in response.content


@pytest.mark.django_db
class TestOAuthCallback:
    """Test OAuth callback handling."""

    def test_callback_creates_new_user(self, oauth_state_session):
        """Callback should create new user if not exists."""
        client, state = oauth_state_session

        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"}
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = {
            "sub": "unique-authentik-id-12345",
            "preferred_username": "newuser",
            "email": "newuser@example.com",
            "groups": ["WCComps_BlueTeam01", "BlueTeam"],
        }
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            response = client.get(f"/auth/callback/?code=test-code&state={state}")

        assert response.status_code == 302  # Redirect after success

        # Verify user created
        user = User.objects.get(username="newuser")
        assert user.email == "newuser@example.com"

        # Verify UserGroups created with correct groups
        user_groups = UserGroups.objects.get(user=user)
        assert user_groups.authentik_id == "unique-authentik-id-12345"
        assert "WCComps_BlueTeam01" in user_groups.groups
        assert "BlueTeam" in user_groups.groups

    def test_callback_updates_existing_user_groups(self, oauth_state_session):
        """Callback should update groups for existing user."""
        client, state = oauth_state_session

        # Create existing user with old groups
        user = User.objects.create_user(username="existinguser", email="existing@example.com")
        UserGroups.objects.create(
            user=user,
            authentik_id="existing-authentik-id",
            groups=["OldGroup"],
        )

        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"}
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = {
            "sub": "existing-authentik-id",
            "preferred_username": "existinguser",
            "email": "existing@example.com",
            "groups": ["WCComps_GoldTeam", "NewGroup"],
        }
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            response = client.get(f"/auth/callback/?code=test-code&state={state}")

        assert response.status_code == 302

        # Verify groups updated
        user_groups = UserGroups.objects.get(user=user)
        assert "WCComps_GoldTeam" in user_groups.groups
        assert "NewGroup" in user_groups.groups
        assert "OldGroup" not in user_groups.groups  # Old groups replaced

    def test_callback_handles_username_change(self, oauth_state_session):
        """Callback should update username if changed in Authentik."""
        client, state = oauth_state_session

        # Create existing user with old username
        user = User.objects.create_user(username="oldusername")
        UserGroups.objects.create(
            user=user,
            authentik_id="user-authentik-id",
            groups=["SomeGroup"],
        )

        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"}
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = {
            "sub": "user-authentik-id",
            "preferred_username": "newusername",  # Username changed
            "groups": ["SomeGroup"],
        }
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            response = client.get(f"/auth/callback/?code=test-code&state={state}")

        assert response.status_code == 302

        # Verify username updated
        user.refresh_from_db()
        assert user.username == "newusername"

    @staticmethod
    def _login(client, state, userinfo, *, id_token=None):
        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"} | (
            {"id_token": id_token} if id_token else {}
        )
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = userinfo
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            return client.get(f"/auth/callback/?code=test-code&state={state}")

    @staticmethod
    def _id_token(auth_time: float) -> str:
        import base64
        import json

        payload = base64.urlsafe_b64encode(json.dumps({"auth_time": int(auth_time)}).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    @pytest.mark.parametrize("before", [True, False])
    def test_a_sign_in_from_before_a_credentials_reset_is_refused(self, oauth_state_session, before):
        """An authorization code issued before the reset must not undo its portal sign-out."""
        import time
        from datetime import UTC, datetime

        client, state = oauth_state_session
        user = User.objects.create(username="team06")
        reset_at = time.time() - 60
        UserGroups.objects.create(user=user, authentik_id="sub-06", groups=[])
        CredentialsReset.objects.create(authentik_id="sub-06", reset_at=datetime.fromtimestamp(reset_at, UTC))
        auth_time = reset_at - 10 if before else reset_at + 10

        response = self._login(
            client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=self._id_token(auth_time)
        )

        assert ("_auth_user_id" in client.session) is not before
        if before:
            assert b"password was reset" in response.content

    def test_without_auth_time_the_sign_in_counts_from_when_it_began(self, oauth_state_session):
        """The login page signed when it started; a reset after that is later than this sign-in."""
        from datetime import timedelta

        from django.utils import timezone

        client, state = oauth_state_session
        user = User.objects.create(username="team06")
        reset_at = timezone.now() + timedelta(seconds=10)
        UserGroups.objects.create(user=user, authentik_id="sub-06", groups=[])
        CredentialsReset.objects.create(authentik_id="sub-06", reset_at=reset_at)

        self._login(client, state, {"sub": "sub-06", "preferred_username": "team06"})

        assert "_auth_user_id" not in client.session

    @pytest.mark.parametrize("auth_time", ["1" + "0" * 400, "NaN", "Infinity", "-Infinity"])
    def test_an_auth_time_that_is_no_moment_counts_from_when_the_sign_in_began(self, oauth_state_session, auth_time):
        import base64
        from datetime import timedelta

        from django.utils import timezone

        client, state = oauth_state_session
        CredentialsReset.objects.create(authentik_id="sub-06", reset_at=timezone.now() + timedelta(seconds=10))
        payload = base64.urlsafe_b64encode(f'{{"auth_time": {auth_time}}}'.encode()).decode().rstrip("=")

        response = self._login(
            client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=f"header.{payload}.signature"
        )

        assert response.status_code == 200
        assert "_auth_user_id" not in client.session

    def test_signing_in_marks_a_disabled_account_enabled(self, oauth_state_session):
        """Authentik signs in only an enabled account, so the next refresh needn't wait to restore permissions."""
        client, state = oauth_state_session
        user = User.objects.create(username="team05")
        UserGroups.objects.create(user=user, authentik_id="sub-05", groups=[], account_active=False)

        self._login(client, state, {"sub": "sub-05", "preferred_username": "team05", "groups": ["WCComps_GoldTeam"]})

        assert UserGroups.objects.get(user=user).permission_groups == ["WCComps_GoldTeam"]

    def test_reused_username_gets_a_fresh_account_not_the_old_one(self, oauth_state_session):
        """Identity is the Authentik sub: a new identity with an old username inherits nothing."""
        from team.models import DiscordLink

        client, state = oauth_state_session
        old = User.objects.create_user(username="reused")
        UserGroups.objects.create(user=old, authentik_id="old-authentik-id", groups=["WCComps_Discord_Admin"])
        DiscordLink.objects.create(discord_id=111, discord_username="old-discord", user=old, is_active=True)

        response = self._login(
            client, state, {"sub": "new-authentik-id", "preferred_username": "reused", "groups": ["WCComps_GoldTeam"]}
        )

        assert response.status_code == 302
        new = User.objects.get(username="reused")
        assert new.pk != old.pk
        assert int(client.session["_auth_user_id"]) == new.pk
        assert UserGroups.objects.get(user=new).authentik_id == "new-authentik-id"
        assert not DiscordLink.objects.filter(user=new).exists()

        old.refresh_from_db()
        assert old.username == f"reused~replaced-{old.pk}"
        # Still tied to its own sub, so the periodic refresh decides its groups
        assert UserGroups.objects.get(user=old).authentik_id == "old-authentik-id"
        assert DiscordLink.objects.get(discord_id=111).user == old

    def test_reused_username_does_not_inherit_django_flags(self, oauth_state_session):
        """A pre-existing account without an Authentik identity (e.g. createsuperuser) isn't reused either."""
        client, state = oauth_state_session
        old = User.objects.create_superuser(username="admin", password="unused-test-password")  # noqa: S106

        self._login(client, state, {"sub": "someone-new", "preferred_username": "admin", "groups": []})

        new = User.objects.get(username="admin")
        assert new.pk != old.pk
        assert not new.is_superuser
        assert not new.is_staff

    def test_a_reused_username_as_long_as_the_column_moves_its_holder_aside_within_it(self, oauth_state_session):
        """The holder's new name keeps as much of the old one as fits beside the suffix."""
        client, state = oauth_state_session
        name = "n" * User._meta.get_field("username").max_length
        old = User.objects.create_user(username=name)
        UserGroups.objects.create(user=old, authentik_id="old-sub", groups=[])

        assert self._login(client, state, {"sub": "new-sub", "preferred_username": name}).status_code == 302

        old.refresh_from_db()
        suffix = f"~replaced-{old.pk}"
        assert old.username == name[: len(name) - len(suffix)] + suffix
        assert User.objects.get(usergroups__authentik_id="new-sub").username == name

    def test_renamed_user_takes_their_new_name_from_a_stale_holder(self, oauth_state_session):
        """Portal username follows Authentik's: /link looks the Authentik user up by it."""
        client, state = oauth_state_session
        me = User.objects.create_user(username="old-name")
        UserGroups.objects.create(user=me, authentik_id="my-id", groups=[])
        stale = User.objects.create_user(username="new-name")
        UserGroups.objects.create(user=stale, authentik_id="deleted-id", groups=["WCComps_GoldTeam"])

        self._login(client, state, {"sub": "my-id", "preferred_username": "new-name", "groups": []})

        me.refresh_from_db()
        stale.refresh_from_db()
        assert me.username == "new-name"
        assert stale.username == f"new-name~replaced-{stale.pk}"
        assert UserGroups.objects.get(user=stale).authentik_id == "deleted-id"

    def test_a_sign_in_begun_at_the_front_page_lands_on_the_roles_own_page(self, oauth_state_session, settings):
        """A login with no page to return to (its next is /) goes to the role's landing page."""
        from django.urls import reverse

        client, state = oauth_state_session

        response = self._login(
            client, state, {"sub": "w", "preferred_username": "w", "groups": [settings.PORTAL_WHITE_GROUP]}
        )

        assert response.status_code == 302
        assert response.url == reverse("leaderboard_page")

    def test_moved_aside_account_takes_its_current_name_on_login(self, oauth_state_session):
        """An account whose name was taken, but whose identity was only renamed, logs in normally."""
        client, state = oauth_state_session
        user = User.objects.create_user(username="carol~replaced-1")
        UserGroups.objects.create(user=user, authentik_id="carol-id", groups=[])

        response = self._login(client, state, {"sub": "carol-id", "preferred_username": "dave", "groups": ["G"]})

        assert response.status_code == 302
        user.refresh_from_db()
        assert user.username == "dave"
        assert client.get("/").wsgi_request.user.pk == user.pk

    def test_callback_rejects_invalid_state(self):
        """Callback should reject request with forged/invalid state."""
        client = Client()
        response = client.get("/auth/callback/?code=test-code&state=forged-state")

        assert response.status_code == 200  # Error page
        assert b"Security Error" in response.content

    def test_callback_rejects_missing_state(self):
        """Callback should reject request without state parameter."""
        client = Client()
        response = client.get("/auth/callback/?code=test-code")

        assert response.status_code == 200  # Error page
        assert b"Session Expired" in response.content

    def test_callback_rejects_expired_state(self):
        """Callback should reject expired state (>5 minutes)."""
        from django.core import signing

        client = Client()
        # Create state with a past timestamp so it appears expired (>5 min old)
        import time
        from unittest.mock import patch as mock_patch

        # Create state, then pretend time has passed
        with mock_patch("django.core.signing.time.time", return_value=time.time() - 600):
            expired_state = signing.dumps({"n": "test-nonce", "next": "/"})

        response = client.get(f"/auth/callback/?code=test-code&state={expired_state}")

        assert response.status_code == 200  # Error page
        assert b"Session Expired" in response.content

    def test_callback_handles_access_denied(self):
        """Callback should handle user cancelling login."""
        client = Client()
        response = client.get("/auth/callback/?error=access_denied")

        assert response.status_code == 200
        assert b"Login Cancelled" in response.content

    def test_callback_logs_user_in(self, oauth_state_session):
        """Callback should log the user in after successful auth."""
        client, state = oauth_state_session

        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"}
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = {
            "sub": "login-test-id",
            "preferred_username": "loginuser",
            "groups": [],
        }
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            client.get(f"/auth/callback/?code=test-code&state={state}")

        # Check user is logged in
        user = User.objects.get(username="loginuser")
        assert "_auth_user_id" in client.session
        assert str(user.id) == client.session["_auth_user_id"]

    def test_callback_redirects_to_next_url(self):
        """Callback should redirect to next URL encoded in the signed state."""
        from urllib.parse import parse_qs, urlparse

        client = Client()

        # Initiate login with a specific next URL
        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            response = client.get("/auth/login/?next=/my-dashboard/")

        parsed = urlparse(response.url)
        state = parse_qs(parsed.query)["state"][0]

        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"}
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = {
            "sub": "redirect-test-id",
            "preferred_username": "redirectuser",
            "groups": [],
        }
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            response = client.get(f"/auth/callback/?code=test-code&state={state}")

        assert response.status_code == 302
        assert response.url == "/my-dashboard/"

    def test_callback_preserves_next_url_with_query_params(self):
        """Callback should preserve next URL containing query parameters (e.g. link token)."""
        from urllib.parse import parse_qs, urlparse

        client = Client()
        next_url = "/auth/link-callback?token=abc123"

        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            response = client.get(f"/auth/login/?next={next_url}")

        parsed = urlparse(response.url)
        state = parse_qs(parsed.query)["state"][0]

        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"}
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = {
            "sub": "link-test-id",
            "preferred_username": "linkuser",
            "groups": [],
        }
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            response = client.get(f"/auth/callback/?code=test-code&state={state}")

        assert response.status_code == 302
        assert response.url == next_url

    def test_callback_rejects_state_from_another_browser(self, oauth_state_session):
        """Login CSRF: a valid state (and code) replayed in a different browser logs nobody in."""
        _client, state = oauth_state_session
        victim = Client()

        response = self._login(victim, state, {"sub": "attacker-sub", "preferred_username": "attacker", "groups": []})

        assert response.status_code == 200
        assert b"Session Expired" in response.content
        assert "_auth_user_id" not in victim.session
        assert not User.objects.filter(username="attacker").exists()

    def test_state_is_single_use(self, oauth_state_session):
        client, state = oauth_state_session
        userinfo = {"sub": "once-sub", "preferred_username": "once", "groups": []}

        assert self._login(client, state, userinfo).status_code == 302
        assert b"Session Expired" in self._login(client, state, userinfo).content

    def test_callback_rejects_external_next_url(self):
        """Callback should reject next URLs pointing to external hosts."""

        from django.core import signing

        client = Client()
        session = client.session
        session["oauth_nonce"] = "test-nonce"
        session.save()

        # Forge a signed state with an external next URL
        state = signing.dumps({"n": "test-nonce", "next": "https://evil.com/steal"})

        mock_token_response = MagicMock()
        mock_token_response.json.return_value = {"access_token": "test-token"}
        mock_token_response.raise_for_status = MagicMock()

        mock_userinfo_response = MagicMock()
        mock_userinfo_response.json.return_value = {
            "sub": "redirect-test-id-2",
            "preferred_username": "safeuser",
            "groups": [],
        }
        mock_userinfo_response.raise_for_status = MagicMock()

        with (
            patch("core.oauth._get_oauth_config") as mock_config,
            patch("core.oauth.httpx.Client") as mock_httpx,
        ):
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            mock_client = MagicMock()
            mock_client.post.return_value = mock_token_response
            mock_client.get.return_value = mock_userinfo_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_httpx.return_value = mock_client

            response = client.get(f"/auth/callback/?code=test-code&state={state}")

        # Should NOT redirect to the external URL
        assert response.status_code == 302
        assert "evil.com" not in response.url


def _authentik_answer(body: object = None, *, failure: Exception | None = None) -> MagicMock:
    answer = MagicMock()
    answer.json.return_value = body
    answer.raise_for_status = MagicMock(side_effect=failure)
    return answer


def _callback_with(client: Client, query: dict[str, str], token: MagicMock, userinfo: MagicMock):
    with (
        patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG),
        patch("core.oauth.httpx.Client") as mock_httpx,
    ):
        mock_client = mock_httpx.return_value.__enter__.return_value
        mock_client.post.return_value = token
        mock_client.get.return_value = userinfo
        return client.get("/auth/callback/", query)


GOOD_TOKEN = {"access_token": "test-token"}
GOOD_USERINFO = {"sub": "refused-sub", "preferred_username": "refused", "groups": []}
HTTP_FAILURE = httpx.HTTPStatusError("502", request=MagicMock(), response=MagicMock())


def _shown(response) -> tuple[int, str, str]:
    """The error page's status, its browser title (the error's, then the site's name) and its message."""
    title = re.search(r"<title>(.*?)</title>", response.content.decode(), re.S)
    assert title, response.content
    return response.status_code, title.group(1).strip(), response.context["error_message"]


@pytest.mark.django_db
class TestEachRefusal:
    """Every way a sign-in fails answers the error page saying why, and signs nobody in."""

    @pytest.fixture(autouse=True)
    def _site_name(self, settings):
        settings.SITE_NAME = "Example Portal"

    @pytest.mark.parametrize(
        ("query", "title", "message"),
        [
            ({"error": "access_denied"}, "Login Cancelled", "You cancelled the login."),
            ({"error": "server_error", "error_description": "down"}, "Login Failed", "Please try again."),
            ({"code": "test-code"}, "Session Expired", "Please try again."),
            ({"code": "test-code", "state": "forged"}, "Security Error", "Please try again."),
        ],
    )
    def test_a_refusal_before_the_state_is_read(self, client, query, title, message):
        response = client.get("/auth/callback/", query)

        assert _shown(response) == (200, f"{title} - Example Portal", message)
        assert "_auth_user_id" not in client.session

    def test_an_expired_state(self, client):
        with patch("django.core.signing.time.time", return_value=time.time() - 600):
            state = signing.dumps({"n": "test-nonce", "next": "/"})

        response = client.get("/auth/callback/", {"code": "test-code", "state": state})

        assert _shown(response) == (200, "Session Expired - Example Portal", "Please try again.")

    def test_a_state_this_browser_did_not_start(self, oauth_state_session):
        _client, state = oauth_state_session
        other = Client()

        response = other.get("/auth/callback/", {"code": "test-code", "state": state})

        assert _shown(response) == (200, "Session Expired - Example Portal", "Please try logging in again.")

    @pytest.mark.parametrize(
        ("query", "token", "userinfo", "message"),
        [
            ({}, GOOD_TOKEN, GOOD_USERINFO, None),
            ({"code": "test-code"}, None, GOOD_USERINFO, "Please try again."),
            ({"code": "test-code"}, {"access_token": ""}, GOOD_USERINFO, "Please try again."),
            ({"code": "test-code"}, {"access_token": 7}, GOOD_USERINFO, "Please try again."),
            ({"code": "test-code"}, GOOD_TOKEN, None, "Please try again."),
            ({"code": "test-code"}, GOOD_TOKEN, {"sub": "refused-sub"}, "Invalid user data."),
        ],
    )
    def test_a_refusal_after_the_state_is_read(self, oauth_state_session, query, token, userinfo, message):
        """No code is a failed login; an exchange or userinfo request that fails, no access token or no usable
        claims is a failed authentication."""
        client, state = oauth_state_session
        token_answer = _authentik_answer(token, failure=HTTP_FAILURE if token is None else None)
        userinfo_answer = _authentik_answer(userinfo, failure=HTTP_FAILURE if userinfo is None else None)

        response = _callback_with(client, {**query, "state": state}, token_answer, userinfo_answer)

        title = "Login Failed" if message is None else "Authentication Failed"
        assert _shown(response) == (200, f"{title} - Example Portal", message or "Please try again.")
        assert "_auth_user_id" not in client.session
        assert not UserGroups.objects.exists()

    @pytest.mark.parametrize("failing", ["token", "userinfo"])
    def test_a_failed_request_to_authentik_is_logged_with_its_error(self, oauth_state_session, caplog, failing):
        client, state = oauth_state_session
        failure = httpx.HTTPStatusError(f"the {failing} endpoint answered 502", request=MagicMock(), response=None)
        token = _authentik_answer(GOOD_TOKEN, failure=failure if failing == "token" else None)
        userinfo = _authentik_answer(GOOD_USERINFO, failure=failure if failing == "userinfo" else None)

        with caplog.at_level("ERROR", logger="core.oauth"):
            _callback_with(client, {"code": "test-code", "state": state}, token, userinfo)

        logged = [r.getMessage() for r in caplog.records if r.name == "core.oauth"]
        assert len(logged) == 1
        assert f"the {failing} endpoint answered 502" in logged[0]

    def test_a_sign_in_from_before_a_reset_offers_a_fresh_sign_in_back_to_its_page(self, client):
        session = client.session
        session["oauth_nonce"] = "reset-nonce"
        session.save()
        state = signing.dumps({"n": "reset-nonce", "next": "/ops/tickets/", "t": int(time.time()) - 60})
        CredentialsReset.objects.create(authentik_id="refused-sub", reset_at=timezone.now())

        response = _callback_with(
            client,
            {"code": "test-code", "state": state},
            _authentik_answer(GOOD_TOKEN),
            _authentik_answer(GOOD_USERINFO),
        )

        assert _shown(response) == (
            200,
            "Signed Out - Example Portal",
            "This account's password was reset after you signed in. Sign in again with it.",
        )
        assert response.context["retry_url"] == "/auth/login/?prompt=login&next=%2Fops%2Ftickets%2F"
        assert "_auth_user_id" not in client.session
        assert not UserGroups.objects.exists()

    def test_authentiks_error_and_its_reason_are_logged(self, client, caplog):
        with caplog.at_level("WARNING", logger="core.oauth"):
            client.get("/auth/callback/", {"error": "server_error", "error_description": "the database is down"})

        assert [r.getMessage() for r in caplog.records if r.name == "core.oauth"] == [
            "OAuth error: server_error - the database is down"
        ]


@pytest.mark.django_db
def test_the_code_is_redeemed_and_the_claims_read_as_authentik_expects(oauth_state_session):
    """The token request carries the grant, the code, the client's credentials and the redirect URI the login sent;
    the userinfo request carries the access token the exchange answered. Both give up after the configured timeout,
    so a hung Authentik can't hold the sign-in open."""
    from urllib.parse import parse_qs

    from django.conf import settings

    client, state = oauth_state_session
    sent: list[httpx.Request] = []
    bodies = [GOOD_TOKEN, GOOD_USERINFO]

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=bodies[len(sent) - 1], request=request)

    with (
        patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG),
        patch.object(httpx.HTTPTransport, "handle_request", handle),
    ):
        response = client.get("/auth/callback/", {"code": "the-code", "state": state})

    assert response.status_code == 302
    token, userinfo = sent
    assert (token.method, str(token.url)) == ("POST", OAUTH_CONFIG["token_endpoint"])
    assert parse_qs(token.content.decode()) == {
        "grant_type": ["authorization_code"],
        "client_id": ["test-client-id"],
        "client_secret": ["test-secret"],
        "code": ["the-code"],
        "redirect_uri": ["http://testserver/auth/callback/"],
    }
    assert (userinfo.method, str(userinfo.url)) == ("GET", OAUTH_CONFIG["userinfo_endpoint"])
    assert userinfo.headers["Authorization"] == "Bearer test-token"
    assert UserGroups.objects.get().authentik_id == GOOD_USERINFO["sub"]
    timeout = settings.HTTPX_DEFAULT_TIMEOUT
    assert (
        token.extensions["timeout"]
        == userinfo.extensions["timeout"]
        == dict.fromkeys(("connect", "read", "write", "pool"), timeout)
    )


SUB = "s" * 64  # UserGroups.authentik_id's column
EMAIL = "e" * 242 + "@example.edu"  # User.email's 254
NAME = "n" * 150  # User.username's


@pytest.mark.django_db
class TestClaims:
    """The account a sign-in makes from Authentik's userinfo claims, or its refusal when they can't make one."""

    def _sign_in(self, oauth_state_session, userinfo):
        client, state = oauth_state_session
        return client, _callback_with(
            client,
            {"code": "test-code", "state": state},
            _authentik_answer(GOOD_TOKEN),
            _authentik_answer(userinfo),
        )

    @pytest.mark.parametrize(
        ("userinfo", "account"),
        [
            (
                {"sub": "a", "preferred_username": "alice", "email": "alice@example.edu", "groups": ["G1", "G2"]},
                ("a", "alice", "alice@example.edu", ["G1", "G2"]),
            ),
            ({"sub": "a", "email": "alice@example.edu"}, ("a", "alice@example.edu", "alice@example.edu", [])),
            (
                {"sub": "a", "preferred_username": "", "email": "x@example.edu"},
                ("a", "x@example.edu", "x@example.edu", []),
            ),
            ({"sub": "a", "preferred_username": "alice", "email": None}, ("a", "alice", "", [])),
            ({"sub": SUB, "preferred_username": NAME, "email": EMAIL}, (SUB, NAME, EMAIL, [])),
        ],
    )
    def test_an_account_from_the_claims(self, oauth_state_session, userinfo, account):
        """The username is the email when there is none; no email is an empty one, no groups none; each claim may
        fill its column."""
        client, response = self._sign_in(oauth_state_session, userinfo)

        assert response.status_code == 302
        row = UserGroups.objects.select_related("user").get()
        assert (row.authentik_id, row.user.username, row.user.email, row.groups) == account
        assert int(client.session["_auth_user_id"]) == row.user.pk

    @pytest.mark.parametrize(
        ("userinfo", "reason"),
        [
            ({"preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": "", "preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": 7, "preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": SUB + "s", "preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": "a\x00", "preferred_username": "alice"}, "a NUL character"),
            ({"sub": "a"}, "no usable username"),
            ({"sub": "a", "preferred_username": 7}, "no usable username"),
            ({"sub": "a", "preferred_username": "al\x00ice"}, "no usable username"),
            ({"sub": "a", "preferred_username": NAME + "n"}, "no usable username"),
            ({"sub": "a", "preferred_username": "alice", "email": 7}, "an unusable email"),
            ({"sub": "a", "preferred_username": "alice", "email": "e" + EMAIL}, "an unusable email"),
            ({"sub": "a", "preferred_username": "alice", "email": "alice\x00@example.edu"}, "a NUL character"),
            ({"sub": "a", "preferred_username": "alice", "groups": "G1"}, "groups that aren't a list of names"),
            ({"sub": "a", "preferred_username": "alice", "groups": None}, "groups that aren't a list of names"),
            ({"sub": "a", "preferred_username": "alice", "groups": ["G1", 7]}, "groups that aren't a list of names"),
            ({"sub": "a", "preferred_username": "alice", "groups": ["G1", "G\x00"]}, "a NUL character"),
        ],
    )
    def test_claims_that_make_no_account_are_refused(self, oauth_state_session, caplog, userinfo, reason):
        """A missing, empty or non-text sub or username, a NUL, a claim past its column, or groups that aren't a
        list of names. The page only says the data was invalid; the log says which claim, for whoever fixes the
        scope mapping."""
        with caplog.at_level("ERROR", logger="core.oauth"):
            client, response = self._sign_in(oauth_state_session, userinfo)

        assert (response.context["error_title"], response.context["error_message"]) == (
            "Authentication Failed",
            "Invalid user data.",
        )
        assert not UserGroups.objects.exists()
        assert "_auth_user_id" not in client.session
        assert [r.getMessage() for r in caplog.records if r.name == "core.oauth"] == [f"Userinfo unusable: {reason}"]


@pytest.mark.django_db
class TestOAuthLogout:
    """Test OAuth logout."""

    def test_logout_clears_session(self):
        """Logout should clear Django session."""
        client = Client()
        user = User.objects.create_user(username="logoutuser")
        client.force_login(user)

        # Verify logged in
        assert "_auth_user_id" in client.session

        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            client.post("/auth/logout/")

        # Verify logged out
        assert "_auth_user_id" not in client.session

    def test_logout_redirects_to_authentik(self):
        """Logout should redirect to Authentik end-session endpoint."""
        client = Client()
        user = User.objects.create_user(username="logoutuser2")
        client.force_login(user)

        with patch("core.oauth._get_oauth_config") as mock_config:
            mock_config.return_value = {
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                "authorization_endpoint": "https://auth.example.com/authorize/",
                "token_endpoint": "https://auth.example.com/token/",
                "userinfo_endpoint": "https://auth.example.com/userinfo/",
                "end_session_endpoint": "https://auth.example.com/end-session/",
            }
            response = client.post("/auth/logout/")

        assert response.status_code == 302
        assert "auth.example.com/end-session" in response.url

    def test_a_get_leaves_the_user_signed_in(self):
        """A link another site plants (or an image) can't sign anyone out."""
        client = Client()
        client.force_login(User.objects.create_user(username="logoutuser3"))

        response = client.get("/auth/logout/")

        assert response.status_code == 405
        assert "_auth_user_id" in client.session

    def test_a_post_without_the_csrf_token_leaves_the_user_signed_in(self):
        """Another site's form posts without the token."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_user(username="logoutuser4"))

        response = client.post("/auth/logout/")

        assert response.status_code == 403
        assert "_auth_user_id" in client.session
