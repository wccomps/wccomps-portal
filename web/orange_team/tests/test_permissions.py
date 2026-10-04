"""Permission tests for orange_team views."""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestDashboardPermissions:
    """Dashboard requires orange_team or gold_team."""

    def test_unauthenticated_redirects(self, unauthenticated_client):
        response = unauthenticated_client.get(reverse("orange_team:dashboard"))
        assert response.status_code == 302

    @pytest.mark.parametrize(
        "user_fixture",
        ["blue_team_user", "red_team_user", "white_team_user", "ticketing_support_user"],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("orange_team:dashboard"))
        assert response.status_code == 302, f"{user_fixture} should be denied"

    @pytest.mark.parametrize("user_fixture", ["orange_team_user", "gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("orange_team:dashboard"))
        assert response.status_code == 200, f"{user_fixture} should have access"


class TestCheckManagementPermissions:
    """Check management requires gold_team only."""

    def test_unauthenticated_redirects(self, unauthenticated_client):
        response = unauthenticated_client.get(reverse("orange_team:check_list"))
        assert response.status_code == 302

    @pytest.mark.parametrize(
        "user_fixture",
        ["blue_team_user", "red_team_user", "white_team_user", "orange_team_user", "ticketing_support_user"],
    )
    def test_unauthorized_roles_denied(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("orange_team:check_list"))
        assert response.status_code == 302, f"{user_fixture} should be denied"

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
    def test_authorized_roles_allowed(self, user_fixture, request):
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("orange_team:check_list"))
        assert response.status_code == 200, f"{user_fixture} should have access"


class TestOrangeLeadAccess:
    """The orange team lead (not gold team) can run the lead pages and the volunteer page."""

    LEAD_PATHS = ["/orange-team/checks/", "/orange-team/review/", "/orange-team/export/"]

    def test_orange_lead_reaches_lead_pages(self, create_user_with_groups):
        user = create_user_with_groups("orangelead", ["WCComps_OrangeTeam_Lead"])
        client = Client()
        client.force_login(user)
        for path in self.LEAD_PATHS:
            assert client.get(path).status_code == 200, path

    def test_plain_orange_member_denied_lead_pages(self, orange_team_user):
        client = Client()
        client.force_login(orange_team_user)
        for path in self.LEAD_PATHS:
            assert client.get(path).status_code == 302, path

    def test_orange_lead_reaches_volunteer_dashboard(self, create_user_with_groups):
        user = create_user_with_groups("orangelead", ["WCComps_OrangeTeam_Lead"])
        client = Client()
        client.force_login(user)
        assert client.get(reverse("orange_team:dashboard")).status_code == 200
        assert client.post(reverse("orange_team:toggle_checkin")).status_code == 302
