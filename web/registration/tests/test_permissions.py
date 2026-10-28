"""Permission tests for registration views."""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestPublicViews:
    """Register is public (no auth required)."""

    def test_register_accessible_unauthenticated(self, unauthenticated_client):
        response = unauthenticated_client.get(reverse("registration_register"))
        assert response.status_code == 200


class TestReviewListPermissions:
    """Review list requires gold_team."""

    def test_unauthenticated_redirects(self, unauthenticated_client):
        response = unauthenticated_client.get(reverse("registration_review_list"))
        assert response.status_code == 302

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
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("registration_review_list"))
        assert response.status_code == 403, f"{user_fixture} should be denied"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("registration_review_list"))
        assert response.status_code == 200, f"{user_fixture} should have access"


class TestSeasonManagementPermissions:
    """Season management requires gold_team."""

    def test_unauthenticated_redirects(self, unauthenticated_client):
        response = unauthenticated_client.get(reverse("registration_season_list"))
        assert response.status_code == 302

    @pytest.mark.parametrize(
        "user_fixture",
        ["blue_team_user", "red_team_user", "white_team_user", "orange_team_user"],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("registration_season_list"))
        assert response.status_code == 403, f"{user_fixture} should be denied"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("registration_season_list"))
        assert response.status_code == 200, f"{user_fixture} should have access"
