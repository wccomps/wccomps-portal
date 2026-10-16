"""Permission integration tests for core views (ops and ticketing).

Uses parametrization to reduce repetition and improve maintainability.
"""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


def _check_access_denied(response) -> bool:
    """Check if response indicates access denied (redirect, 403, or soft 200 with error)."""
    if response.status_code in (302, 403):
        return True
    content_lower = response.content.lower()
    return b"access denied" in content_lower or b"you do not have permission" in content_lower


class TestOpsSchoolInfoPermissions:
    """Test permissions for School Info view (/ops/school-info/)."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("school_info"))
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
        """Non-gold/admin users should be denied access to school info."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("school_info"))
        assert _check_access_denied(response), f"{user_fixture} should be denied access"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        """Gold team and admin users should access school info."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("school_info"))
        assert response.status_code == 200
        assert not _check_access_denied(response), f"{user_fixture} should have access"


class TestTicketViewsPermissions:
    """Test permissions for ticketing views."""

    def test_team_tickets_unauthenticated_redirects(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("ticket_list"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    def test_ops_tickets_unauthenticated_redirects(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("ticket_list"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    @pytest.mark.parametrize(
        "user_fixture",
        [
            "blue_group_member_without_team",
            "red_team_user",
            "gold_team_user",
            "white_team_user",
            "orange_team_user",
        ],
    )
    def test_ops_tickets_unauthorized_roles_denied(self, user_fixture, request):
        """Non-ticketing users should get 403 on ops tickets."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("ticket_list"))
        assert response.status_code == 403, f"{user_fixture} should get 403"

    @pytest.mark.parametrize(
        "user_fixture",
        ["ticketing_support_user", "ticketing_admin_user", "admin_user"],
    )
    def test_ops_tickets_authorized_roles_allowed(self, user_fixture, request):
        """Ticketing staff and admin should access ops tickets."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("ticket_list"))
        assert response.status_code == 200, f"{user_fixture} should have access"
