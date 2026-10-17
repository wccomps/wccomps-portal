"""
Tests for old Orange Team Portal redirect (formerly PERM-4).

The old orange team portal now redirects to the orange_team dashboard.
These tests verify the redirect works correctly.
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from core.models import UserGroups


class OrangeTeamRedirectRedirectTests(TestCase):
    """Test that old Orange Team portal redirects to orange_team dashboard."""

    def setUp(self) -> None:
        """Set up test data."""
        # Create Orange Team user
        self.orange_user = User.objects.create_user(username="orange1", password="test123")
        UserGroups.objects.create(user=self.orange_user, authentik_id="orange1-uid", groups=["WCComps_OrangeTeam"])

        # Create Gold Team user
        self.gold_user = User.objects.create_user(username="gold1", password="test123")
        UserGroups.objects.create(user=self.gold_user, authentik_id="gold1-uid", groups=["WCComps_GoldTeam"])

        self.client = Client()

    def test_orange_team_redirect_goes_to_dashboard(self) -> None:
        """Orange Team portal redirects to orange_team dashboard."""
        self.client.force_login(User.objects.get(username="orange1"))
        response = self.client.get(reverse("scoring:orange_team_redirect"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("orange_team:dashboard"))

    def test_submit_orange_check_redirects(self) -> None:
        """Submit orange check redirects to orange_team dashboard."""
        self.client.force_login(User.objects.get(username="orange1"))
        response = self.client.get(reverse("scoring:submit_orange_check"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("orange_team:dashboard"))

    def test_gold_team_redirects(self) -> None:
        """Gold Team accessing old portal also redirects."""
        self.client.force_login(User.objects.get(username="gold1"))
        response = self.client.get(reverse("scoring:orange_team_redirect"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("orange_team:dashboard"))
