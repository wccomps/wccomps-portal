"""Permission tests for registration views."""

import pytest
from django.test import Client
from django.urls import reverse

from registration.models import RegistrationContact, RegistrationEventEnrollment, TeamRegistration
from team.models import SchoolInfo, Team

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


@pytest.fixture
def assignable(current_event):
    """The open event with one approved, enrolled registration that has a captain, and a free team for it."""
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    RegistrationEventEnrollment.objects.create(registration=registration, event=current_event)
    RegistrationContact.objects.create(registration=registration, role="captain", name="C", email="c@example.edu")
    Team.objects.create(team_number=4, team_name="Team 04")
    return current_event


class TestReviewActionPermissions:
    """Approve, reject and assign teams change registrations and the school list: gold_team only, by POST."""

    @pytest.mark.parametrize("user_fixture", ["blue_team_user", "white_team_user", "orange_team_user"])
    @pytest.mark.parametrize("action", ["registration_approve", "registration_reject"])
    def test_other_roles_are_refused_review_and_the_registration_stays_pending(self, user_fixture, action, request):
        registration = TeamRegistration.objects.create(school_name="Example University")
        client = Client()
        client.force_login(request.getfixturevalue(user_fixture))

        response = client.post(reverse(action, args=[registration.pk]), {"reason": "Late"})

        assert response.status_code == 403
        assert TeamRegistration.objects.get().status == "pending"

    @pytest.mark.parametrize("user_fixture", ["blue_team_user", "white_team_user", "orange_team_user"])
    def test_other_roles_are_refused_assigning_teams(self, user_fixture, assignable, request):
        client = Client()
        client.force_login(request.getfixturevalue(user_fixture))

        response = client.post(reverse("registration_assign_teams", args=[assignable.pk]))

        assert response.status_code == 403
        assert not SchoolInfo.objects.exists()

    def test_approving_by_get_is_refused(self, gold_team_user):
        registration = TeamRegistration.objects.create(school_name="Example University")
        client = Client()
        client.force_login(gold_team_user)

        response = client.get(reverse("registration_approve", args=[registration.pk]))

        assert response.status_code == 405
        assert TeamRegistration.objects.get().status == "pending"

    def test_assigning_teams_by_get_is_refused(self, gold_team_user, assignable):
        client = Client()
        client.force_login(gold_team_user)

        response = client.get(reverse("registration_assign_teams", args=[assignable.pk]))

        assert response.status_code == 405
        assert not SchoolInfo.objects.exists()

    def test_reject_by_get_only_asks_for_a_reason(self, gold_team_user):
        registration = TeamRegistration.objects.create(school_name="Example University")
        client = Client()
        client.force_login(gold_team_user)

        response = client.get(reverse("registration_reject", args=[registration.pk]), {"reason": "Late"})

        assert response.status_code == 200
        assert 'name="reason"' in response.content.decode()
        assert not list(response.context["messages"])
        assert TeamRegistration.objects.get().status == "pending"
