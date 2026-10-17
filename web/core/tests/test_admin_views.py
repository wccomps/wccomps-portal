"""Tests for admin views (competition, teams, helpers, broadcast).

These tests verify that admin views render correctly and catch template errors.
"""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


def _check_access_denied(response) -> bool:
    """Check if response indicates access denied (soft 200 with error message)."""
    content_lower = response.content.lower()
    return b"access denied" in content_lower or b"you do not have permission" in content_lower


class TestAdminCompetitionView:
    """Test admin competition management view."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("admin_competition"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    @pytest.mark.parametrize(
        "user_fixture",
        [
            "blue_team_user",
            "red_team_user",
            "white_team_user",
            "orange_team_user",
            "ticketing_support_user",
            "ticketing_admin_user",
        ],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        """Non-gold/admin users should be redirected by @require_permission."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_competition"))
        assert response.status_code == 403, f"{user_fixture} should be refused"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        """Gold team and admin users should access competition management."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_competition"))
        assert response.status_code == 200
        assert not _check_access_denied(response), f"{user_fixture} should have access"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_template_renders_without_error(self, user_fixture, request):
        """Verify template renders completely without template errors."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_competition"))
        assert response.status_code == 200
        assert b'id="event-panel"' in response.content


class TestAdminTeamsView:
    """Test admin teams management view."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("admin_teams"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    @pytest.mark.parametrize(
        "user_fixture",
        [
            "blue_team_user",
            "red_team_user",
            "white_team_user",
            "orange_team_user",
            "ticketing_support_user",
            "ticketing_admin_user",
        ],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        """Non-gold/admin users should be redirected by @require_permission."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_teams"))
        assert response.status_code == 403, f"{user_fixture} should be refused"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        """Gold team and admin users should access teams management."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_teams"))
        assert response.status_code == 200
        assert not _check_access_denied(response), f"{user_fixture} should have access"


class TestAdminTeamDetailView:
    """Test admin team detail view."""

    @pytest.fixture
    def test_team(self, db):
        """Create a test team for detail view tests."""
        from team.models import Team

        team, _ = Team.objects.get_or_create(
            team_number=1,
            defaults={"team_name": "Team 01", "is_active": True},
        )
        return team

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_team_detail_renders(self, user_fixture, request, test_team):
        """Verify team detail page renders without template errors."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_team_detail", kwargs={"team_number": test_team.team_number}))
        assert response.status_code == 200
        assert not _check_access_denied(response)

    def test_nonexistent_team_returns_error(self, admin_user):
        """Requesting nonexistent team should return error page."""
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("admin_team_detail", kwargs={"team_number": 999}))
        assert response.status_code == 404
        assert b"not found" in response.content.lower()


class TestAdminBroadcastView:
    """Test admin broadcast view."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("admin_broadcast"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    @pytest.mark.parametrize(
        "user_fixture",
        [
            "blue_team_user",
            "red_team_user",
            "white_team_user",
            "orange_team_user",
            "ticketing_support_user",
            "ticketing_admin_user",
        ],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        """Non-gold/admin users should be redirected by @require_permission."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_broadcast"))
        assert response.status_code == 403, f"{user_fixture} should be refused"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        """Gold team and admin users should access broadcast."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_broadcast"))
        assert response.status_code == 200
        assert not _check_access_denied(response), f"{user_fixture} should have access"


class TestAdminSyncRolesView:
    """Test admin sync roles view."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("admin_sync_roles"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        """Gold team and admin users should access sync roles."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_sync_roles"))
        assert response.status_code == 200
        assert not _check_access_denied(response), f"{user_fixture} should have access"
