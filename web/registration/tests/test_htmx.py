"""Tests for htmx partial responses in registration views."""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


class TestRegistrationReviewListHtmx:
    """Tests for registration_review_list htmx partial responses."""

    def test_htmx_request_returns_partial(self, gold_team_user):
        """htmx request returns only the registration table partial."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(
            reverse("registration_review_list") + "?status=pending",
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert 'id="registration-review-content"' in content
        assert "<title>" not in content

    def test_regular_request_returns_full_page(self, gold_team_user):
        """Regular request returns full page."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("registration_review_list"))
        assert response.status_code == 200
        content = response.content.decode()
        assert "<title>" in content
        assert "Team Registration Review" in content
