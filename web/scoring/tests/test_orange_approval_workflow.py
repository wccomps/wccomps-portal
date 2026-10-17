"""
Tests for Orange Team check approval workflow (FEAT-4).

Tests cover bulk approval, the review page, and permissions.
Individual approve/reject views have been removed in favor of
bulk-only approval via the review page.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from core.models import UserGroups
from scoring.models import OrangeTeamScore
from team.models import Team


class BulkOrangeApprovalTests(TestCase):
    """Test bulk approval of orange team checks."""

    def setUp(self) -> None:
        """Set up test data."""
        # Create users
        self.gold_user = User.objects.create_user(username="gold", password="test123")
        self.orange_user = User.objects.create_user(username="orange", password="test123")

        # Create UserGroups for permissions
        UserGroups.objects.create(user=self.gold_user, authentik_id="gold-uid", groups=["WCComps_GoldTeam"])
        UserGroups.objects.create(user=self.orange_user, authentik_id="orange-uid", groups=["WCComps_OrangeTeam"])

        # Create teams
        self.team1 = Team.objects.create(team_number=1, team_name="Team 1")
        self.team2 = Team.objects.create(team_number=2, team_name="Team 2")

        # Create multiple unapproved adjustments
        self.adj1 = OrangeTeamScore.objects.create(
            team=self.team1,
            description="Adjustment 1",
            points_awarded=Decimal("50.00"),
            submitted_by=self.orange_user,
        )
        self.adj2 = OrangeTeamScore.objects.create(
            team=self.team2,
            description="Adjustment 2",
            points_awarded=Decimal("30.00"),
            submitted_by=self.orange_user,
        )
        self.adj3 = OrangeTeamScore.objects.create(
            team=self.team1,
            description="Adjustment 3",
            points_awarded=Decimal("-20.00"),
            submitted_by=self.orange_user,
        )

        self.client = Client()

    def test_bulk_approve_multiple_adjustments(self) -> None:
        """Can bulk approve multiple adjustments at once."""
        self.client.force_login(User.objects.get(username="gold"))
        url = reverse("scoring:bulk_approve_orange_adjustments")

        response = self.client.post(url, {"adjustment_ids": [self.adj1.id, self.adj2.id]})

        self.assertEqual(response.status_code, 302)
        self.adj1.refresh_from_db()
        self.adj2.refresh_from_db()
        self.adj3.refresh_from_db()

        self.assertTrue(self.adj1.is_approved)
        self.assertTrue(self.adj2.is_approved)
        self.assertFalse(self.adj3.is_approved)  # Not in selection

    def test_bulk_approve_sets_approver_for_all(self) -> None:
        """Bulk approve sets the approver user for all adjustments."""
        self.client.force_login(User.objects.get(username="gold"))
        url = reverse("scoring:bulk_approve_orange_adjustments")

        response = self.client.post(url, {"adjustment_ids": [self.adj1.id, self.adj2.id]})

        self.assertEqual(response.status_code, 302)
        self.adj1.refresh_from_db()
        self.adj2.refresh_from_db()

        self.assertEqual(self.adj1.approved_by, self.gold_user)
        self.assertEqual(self.adj2.approved_by, self.gold_user)

    def test_bulk_approve_leaves_already_approved_alone(self) -> None:
        """Re-approving keeps the original approver and time."""
        from django.utils import timezone

        approved_at = timezone.now() - timezone.timedelta(hours=1)
        OrangeTeamScore.objects.filter(pk=self.adj1.pk).update(
            is_approved=True, approved_by=self.orange_user, approved_at=approved_at
        )
        self.client.force_login(User.objects.get(username="gold"))

        self.client.post(reverse("scoring:bulk_approve_orange_adjustments"), {"adjustment_ids": [self.adj1.id]})

        self.adj1.refresh_from_db()
        self.assertEqual((self.adj1.approved_by, self.adj1.approved_at), (self.orange_user, approved_at))

    def test_bulk_approve_empty_selection(self) -> None:
        """Bulk approve with empty selection returns gracefully."""
        self.client.force_login(User.objects.get(username="gold"))
        url = reverse("scoring:bulk_approve_orange_adjustments")

        response = self.client.post(url, {"adjustment_ids": []})

        # Should redirect back without error
        self.assertEqual(response.status_code, 302)

    def test_bulk_approve_invalid_ids_skipped(self) -> None:
        """Bulk approve skips invalid adjustment IDs."""
        self.client.force_login(User.objects.get(username="gold"))
        url = reverse("scoring:bulk_approve_orange_adjustments")

        response = self.client.post(url, {"adjustment_ids": [self.adj1.id, 99999]})

        self.assertEqual(response.status_code, 302)
        self.adj1.refresh_from_db()
        self.assertTrue(self.adj1.is_approved)

    def test_orange_team_can_bulk_approve(self) -> None:
        """Orange Team members can bulk approve their own checks."""
        self.client.force_login(User.objects.get(username="orange"))
        url = reverse("scoring:bulk_approve_orange_adjustments")

        response = self.client.post(url, {"adjustment_ids": [self.adj1.id, self.adj2.id]})

        self.assertEqual(response.status_code, 302)
        self.adj1.refresh_from_db()
        self.adj2.refresh_from_db()
        self.assertTrue(self.adj1.is_approved)
        self.assertTrue(self.adj2.is_approved)

    def test_bulk_approve_only_accepts_post(self) -> None:
        """Bulk approve endpoint only accepts POST requests."""
        self.client.force_login(User.objects.get(username="gold"))
        url = reverse("scoring:bulk_approve_orange_adjustments")

        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

    def test_bulk_approve_redirects_to_review_orange(self) -> None:
        """Bulk approve redirects to the review orange page."""
        self.client.force_login(User.objects.get(username="gold"))
        url = reverse("scoring:bulk_approve_orange_adjustments")

        response = self.client.post(url, {"adjustment_ids": [self.adj1.id]})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("scoring:review_orange"))


class ReviewOrangePageTests(TestCase):
    """Test the review orange page view."""

    def setUp(self) -> None:
        """Set up test data."""
        self.orange_user = User.objects.create_user(username="orange", password="test123")
        self.gold_user = User.objects.create_user(username="gold", password="test123")
        self.blue_user = User.objects.create_user(username="blue", password="test123")

        UserGroups.objects.create(user=self.orange_user, authentik_id="orange-uid", groups=["WCComps_OrangeTeam"])
        UserGroups.objects.create(user=self.gold_user, authentik_id="gold-uid", groups=["WCComps_GoldTeam"])
        UserGroups.objects.create(user=self.blue_user, authentik_id="blue-uid", groups=["WCComps_BlueTeam01"])

        self.team = Team.objects.create(team_number=1, team_name="Team 1")

        self.client = Client()

    def test_orange_team_can_access_review_page(self) -> None:
        """Orange team members can access the review page."""
        self.client.force_login(User.objects.get(username="orange"))
        response = self.client.get(reverse("scoring:review_orange"))
        self.assertEqual(response.status_code, 200)

    def test_gold_team_can_access_review_page(self) -> None:
        """Gold team members can access the review page."""
        self.client.force_login(User.objects.get(username="gold"))
        response = self.client.get(reverse("scoring:review_orange"))
        self.assertEqual(response.status_code, 200)

    def test_blue_team_cannot_access_review_page(self) -> None:
        """Blue team members cannot access the review page."""
        self.client.force_login(User.objects.get(username="blue"))
        response = self.client.get(reverse("scoring:review_orange"))
        self.assertIn(response.status_code, [302, 403])

    def test_review_page_defaults_to_pending_filter(self) -> None:
        """Review page defaults to showing pending checks."""
        OrangeTeamScore.objects.create(
            team=self.team,
            description="Pending check",
            points_awarded=Decimal("50.00"),
            submitted_by=self.orange_user,
            is_approved=False,
        )
        OrangeTeamScore.objects.create(
            team=self.team,
            description="Approved check",
            points_awarded=Decimal("30.00"),
            submitted_by=self.orange_user,
            is_approved=True,
        )

        self.client.force_login(User.objects.get(username="orange"))
        response = self.client.get(reverse("scoring:review_orange"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["filters"]["status"], "pending")
        self.assertEqual(len(response.context["page_obj"]), 1)

    def test_review_page_htmx_returns_table_only(self) -> None:
        """HTMX request returns just the table partial."""
        self.client.force_login(User.objects.get(username="orange"))
        response = self.client.get(
            reverse("scoring:review_orange"),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        # HTMX response should use the table template, not the full page
        self.assertTemplateUsed(response, "scoring/partials/review_orange_table.html")
