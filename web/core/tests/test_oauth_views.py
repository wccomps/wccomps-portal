"""Tests for OAuth/Discord linking views."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from core.models import CompetitionConfig
from team.models import DiscordLink, LinkAttempt, LinkToken


def _set_member_limit(limit: int) -> None:
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"max_team_members": limit})


pytestmark = pytest.mark.django_db


class TestLinkInitiate:
    """Tests for link_initiate view (OAuth flow start)."""

    def test_missing_token_returns_400(self):
        """Request without token parameter should return 400."""
        client = Client()
        response = client.get("/auth/link")
        assert response.status_code == 400
        assert b"Missing token" in response.content

    def test_invalid_token_shows_error_page(self):
        """Invalid token should show error page."""
        client = Client()
        response = client.get("/auth/link?token=invalid_token_12345")
        assert response.status_code == 200
        assert b"Invalid or expired token" in response.content

    def test_used_token_shows_error_page(self):
        """Already used token should show error page."""
        token = LinkToken.objects.create(
            token="used_token_123",
            discord_id=123456789,
            discord_username="testuser",
            used=True,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        client = Client()
        response = client.get(f"/auth/link?token={token.token}")
        assert response.status_code == 200
        assert b"Invalid or expired token" in response.content

    def test_expired_token_shows_error_page(self):
        """Expired token should show error page."""
        token = LinkToken.objects.create(
            token="expired_token_123",
            discord_id=123456789,
            discord_username="testuser",
            used=False,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        client = Client()
        response = client.get(f"/auth/link?token={token.token}")
        assert response.status_code == 200
        assert b"Token expired" in response.content

    def test_valid_token_redirects_to_oauth(self):
        """Valid token should redirect to Authentik OAuth."""
        token = LinkToken.objects.create(
            token="valid_token_abc123",
            discord_id=123456789,
            discord_username="testuser",
            used=False,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        client = Client()
        response = client.get(f"/auth/link?token={token.token}")
        assert response.status_code == 302
        assert "/auth/login/" in response.url
        from urllib.parse import unquote

        assert f"token={token.token}" in unquote(response.url)

    def test_valid_token_stores_in_session(self):
        """Valid token should be stored in session for CSRF protection."""
        token = LinkToken.objects.create(
            token="session_token_xyz",
            discord_id=123456789,
            discord_username="testuser",
            used=False,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        client = Client()
        client.get(f"/auth/link?token={token.token}")
        assert client.session.get("pending_link_token") == token.token
        assert client.session.get("pending_link_discord_id") == token.discord_id


class TestLinkCallback:
    """Tests for link_callback view (OAuth callback handling)."""

    def test_unauthenticated_redirects_to_login(self):
        """Unauthenticated users should be redirected to login."""
        client = Client()
        response = client.get("/auth/link-callback")
        assert response.status_code == 302
        assert "/auth/login" in response.url

    def test_missing_token_in_url_shows_error(self, blue_team_user):
        """Callback without token in URL should show error."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get("/auth/link-callback")
        assert response.status_code == 200
        assert b"Missing authentication state" in response.content

    def test_csrf_mismatch_shows_error(self, blue_team_user):
        """Token mismatch between session and URL should show error."""
        token = LinkToken.objects.create(
            token="csrf_test_token",
            discord_id=123456789,
            discord_username="testuser",
            used=False,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        client = Client()
        client.force_login(blue_team_user)
        # Session has different token
        session = client.session
        session["pending_link_token"] = "different_token"
        session.save()
        response = client.get(f"/auth/link-callback?token={token.token}")
        assert response.status_code == 200
        assert b"opened in a different browser" in response.content

    def test_successful_team_link(self, blue_team_user, blue_team):
        """Successful team account linking creates DiscordLink and tasks."""
        from unittest.mock import patch

        team = blue_team
        token = LinkToken.objects.create(
            token="success_token_123",
            discord_id=987654321,
            discord_username="discorduser",
            used=False,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        client = Client()
        client.force_login(blue_team_user)
        session = client.session
        session["pending_link_token"] = token.token
        session.save()

        # GET only asks for confirmation; nothing is linked yet
        response = client.get(f"/auth/link-callback?token={token.token}")
        assert response.status_code == 200
        assert b"Confirm Discord Link" in response.content
        assert b"discorduser" in response.content
        assert not DiscordLink.objects.filter(discord_id=987654321).exists()

        # Mock the Authentik API call to avoid real API errors
        with patch("core.authentik_manager.AuthentikManager"):
            response = client.post("/auth/link-callback", {"token": token.token})

        assert response.status_code == 200
        assert b"Successfully Linked" in response.content

        # Verify link created
        link = DiscordLink.objects.get(discord_id=987654321)
        assert link.is_active
        assert link.team == team

        # Verify token marked used
        token.refresh_from_db()
        assert token.used

        # Verify attempt logged
        attempt = LinkAttempt.objects.get(discord_id=987654321)
        assert attempt.success

    def test_team_full_prevents_linking(self, blue_team_user, blue_team):
        """Cannot link when team is at max capacity."""
        from unittest.mock import patch

        _set_member_limit(1)
        team = blue_team
        # Fill the team
        existing_user = User.objects.create_user(username="existinguser")
        DiscordLink.objects.create(
            discord_id=111111111,
            discord_username="existinguser",
            user=existing_user,
            team=team,
            is_active=True,
        )
        token = LinkToken.objects.create(
            token="full_team_token",
            discord_id=987654321,
            discord_username="discorduser",
            used=False,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        client = Client()
        client.force_login(blue_team_user)
        session = client.session
        session["pending_link_token"] = token.token
        session.save()

        # Mock the Authentik API call to avoid real API errors
        with patch("core.authentik_manager.AuthentikManager"):
            response = client.post("/auth/link-callback", {"token": token.token})

        assert response.status_code == 200
        assert b"Team full" in response.content

        # Verify failed attempt logged
        attempt = LinkAttempt.objects.get(discord_id=987654321)
        assert not attempt.success
        assert "full" in attempt.failure_reason.lower()

    def test_member_of_full_team_can_relink(self, db, blue_team_user, blue_team):
        """Their own current link is the one being replaced, so it doesn't count toward the limit."""
        from unittest.mock import patch

        _set_member_limit(1)
        team = blue_team
        DiscordLink.objects.create(
            discord_id=987654321, discord_username="discorduser", user=blue_team_user, team=team, is_active=True
        )
        token = LinkToken.objects.create(
            token="relink_token",
            discord_id=987654321,
            discord_username="discorduser",
            used=False,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        client = Client()
        client.force_login(blue_team_user)
        session = client.session
        session["pending_link_token"] = token.token
        session.save()

        with patch("core.authentik_manager.AuthentikManager"):
            response = client.post("/auth/link-callback", {"token": token.token})

        assert b"Team full" not in response.content
        assert DiscordLink.objects.filter(discord_id=987654321, team=team, is_active=True).count() == 1


class TestLinkHijackProtection:
    """A /link URL made by someone else must not link their Discord account to the viewer's account."""

    @pytest.fixture
    def attacker_token(self):
        return LinkToken.objects.create(
            token="attacker_token_abc",
            discord_id=555000555,
            discord_username="attacker",
            used=False,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

    def test_callback_without_session_token_is_refused(self, gold_team_user, attacker_token):
        """Victim sent straight to link-callback (browser never started the flow)."""
        client = Client()
        client.force_login(gold_team_user)

        for response in (
            client.get(f"/auth/link-callback?token={attacker_token.token}"),
            client.post("/auth/link-callback", {"token": attacker_token.token}),
        ):
            assert b"opened in a different browser" in response.content
        assert not DiscordLink.objects.filter(discord_id=attacker_token.discord_id).exists()

    def test_opening_a_shared_link_only_shows_confirmation(self, gold_team_user, attacker_token):
        """Victim opens the attacker's /auth/link URL: they must see whose Discord account it is."""
        client = Client()
        client.force_login(gold_team_user)

        # The shared URL starts the flow in the victim's own browser, then SSO lands on the callback
        start = client.get(f"/auth/link?token={attacker_token.token}")
        assert start.status_code == 302 and start.url.startswith("/auth/login/")
        response = client.get(f"/auth/link-callback?token={attacker_token.token}")

        assert b"Confirm Discord Link" in response.content
        assert b"attacker" in response.content
        assert b"only continue if you ran /link yourself" in response.content.lower()
        assert not DiscordLink.objects.filter(discord_id=attacker_token.discord_id).exists()

    def test_confirm_post_requires_csrf_token(self, gold_team_user, attacker_token):
        client = Client(enforce_csrf_checks=True)
        client.force_login(gold_team_user)
        session = client.session
        session["pending_link_token"] = attacker_token.token
        session.save()

        response = client.post("/auth/link-callback", {"token": attacker_token.token})

        assert response.status_code == 403
        assert not DiscordLink.objects.filter(discord_id=attacker_token.discord_id).exists()

    def test_confirm_page_form_carries_csrf_token(self, gold_team_user, attacker_token):
        client = Client()
        client.force_login(gold_team_user)
        session = client.session
        session["pending_link_token"] = attacker_token.token
        session.save()

        html = client.get(f"/auth/link-callback?token={attacker_token.token}").content.decode()

        form = html[html.index("<form") : html.index("</form>")]
        assert 'method="post"' in form
        assert "csrfmiddlewaretoken" in form
        assert attacker_token.token in form


class TestLinkTokenExpiry:
    """Test token expiry edge cases."""

    def test_token_just_expired(self):
        """Token that expired 1 second ago should be rejected."""
        token = LinkToken.objects.create(
            token="just_expired_token",
            discord_id=123456789,
            discord_username="testuser",
            used=False,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        client = Client()
        response = client.get(f"/auth/link?token={token.token}")
        assert b"expired" in response.content.lower()

    def test_token_about_to_expire(self):
        """Token that expires in 1 second should still work."""
        token = LinkToken.objects.create(
            token="about_to_expire_token",
            discord_id=123456789,
            discord_username="testuser",
            used=False,
            expires_at=timezone.now() + timedelta(seconds=5),
        )
        client = Client()
        response = client.get(f"/auth/link?token={token.token}")
        assert response.status_code == 302  # Redirects to OAuth


class TestDiscordLinkConstraints:
    """Test DiscordLink uniqueness and constraints."""

    def test_only_one_active_link_per_discord_user(self, blue_team_user, blue_team):
        """Calling deactivate_previous_links before creating new link deactivates previous active link."""
        team = blue_team
        discord_id = 123456789
        user1 = User.objects.create_user(username="auth1")
        user2 = User.objects.create_user(username="auth2")

        # Create first link
        link1 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="user1",
            user=user1,
            team=team,
            is_active=True,
        )

        # Explicitly deactivate previous links before creating second link
        DiscordLink.deactivate_previous_links(discord_id)

        # Create second link for same Discord user
        link2 = DiscordLink.objects.create(
            discord_id=discord_id,
            discord_username="user1_updated",
            user=user2,
            team=team,
            is_active=True,
        )

        link1.refresh_from_db()
        assert not link1.is_active
        assert link1.unlinked_at is not None
        assert link2.is_active

    def test_multiple_discord_users_can_link_to_team_account(self, blue_team):
        """Multiple Discord users can link to same team Authentik account."""
        team = blue_team
        # Blue teams share a single Authentik account
        team_user = User.objects.create_user(username="team01")

        link1 = DiscordLink.objects.create(
            discord_id=111111111,
            discord_username="user1",
            user=team_user,
            team=team,
            is_active=True,
        )
        link2 = DiscordLink.objects.create(
            discord_id=222222222,
            discord_username="user2",
            user=team_user,
            team=team,
            is_active=True,
        )

        # Both should remain active
        link1.refresh_from_db()
        link2.refresh_from_db()
        assert link1.is_active
        assert link2.is_active
