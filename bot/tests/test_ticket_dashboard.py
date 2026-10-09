"""Tests for ticket dashboard functionality."""

from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import User
from django.test import TestCase

from bot.ticket_dashboard import (
    format_ticket_embed,
    trigger_dashboard,
)
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory


@pytest.mark.django_db(transaction=True)
class TicketDashboardTest(TestCase):
    """Test ticket dashboard embed generation."""

    def setUp(self) -> None:
        self.team = Team.objects.create(
            team_number=40,
            team_name="Test Team",
            authentik_group="test-group",
        )
        for pk, name, pts in [
            (1, "Service Scoring Validation", 0),
            (2, "Box Reset / Scrub", 60),
            (3, "Scoring Service Check", 10),
            (4, "Black Team Phone Consultation", 100),
            (5, "Red Team Activity Review", 0),
            (6, "General Inquiry", 0),
        ]:
            TicketCategory.objects.get_or_create(pk=pk, defaults={"display_name": name, "points": pts})

    def test_format_ticket_embed_comprehensive(self) -> None:
        """Test formatting ticket embeds with various states and categories."""
        from django.utils import timezone as tz

        from core.tickets_config import get_all_categories

        # Test basic open ticket
        box_reset, _ = TicketCategory.objects.get_or_create(
            pk=2,
            defaults={"display_name": "Box Reset", "points": 60},
        )
        open_ticket = Ticket.objects.create(
            ticket_number="T001-001",
            team=self.team,
            category=box_reset,
            title="Test Open",
            description="Open description",
            status="open",
        )
        open_embed = format_ticket_embed(open_ticket)
        self.assertIsNotNone(open_embed)
        self.assertIn("Test Open", str(open_embed.title))

        # Create DiscordLink for assignment testing
        volunteer_user = User.objects.create(username="volunteer1")
        DiscordLink.objects.create(
            user=volunteer_user,
            discord_id=123456789,
            discord_username="volunteer1",
        )

        # Test claimed ticket with assignment (assigned_to is now User, not DiscordLink)
        scoring_check, _ = TicketCategory.objects.get_or_create(
            pk=3,
            defaults={"display_name": "Scoring Service Check", "points": 10},
        )
        claimed_ticket = Ticket.objects.create(
            ticket_number="T001-002",
            team=self.team,
            category=scoring_check,
            title="Test Claimed",
            description="Claimed description",
            status="claimed",
            assigned_to=volunteer_user,
        )
        claimed_embed = format_ticket_embed(claimed_ticket)
        field_names = [field.name for field in claimed_embed.fields]
        self.assertIn("Assigned To", field_names)

        # Create user for resolver
        admin_user = User.objects.create(username="admin1")
        DiscordLink.objects.create(
            user=admin_user,
            discord_id=987654321,
            discord_username="admin1",
        )

        # Test resolved ticket with resolution info (resolved_by is now User, not DiscordLink)
        other_cat = TicketCategory.objects.get(pk=6)
        resolved_ticket = Ticket.objects.create(
            ticket_number="T001-003",
            team=self.team,
            category=other_cat,
            title="Test Resolved",
            description="Resolved description",
            status="resolved",
            resolved_at=tz.now(),
            resolved_by=admin_user,
            resolution_notes="All fixed",
            points_charged=10,
        )
        resolved_embed = format_ticket_embed(resolved_ticket)
        resolved_field_names = [field.name for field in resolved_embed.fields]
        self.assertIn("Resolved At", resolved_field_names)

        # Test that all ticket categories can be formatted
        all_categories = get_all_categories()
        for idx, (cat_pk, cat_info) in enumerate(all_categories.items(), start=10):
            cat_obj = TicketCategory.objects.get(pk=cat_pk)
            ticket = Ticket.objects.create(
                ticket_number=f"T001-{idx:03d}",
                team=self.team,
                category=cat_obj,
                title=f"Test {cat_info['display_name']}",
                description="Test",
                status="open",
            )
            embed = format_ticket_embed(ticket)
            self.assertIsNotNone(embed)

    def test_format_ticket_embed_includes_category_fields(self) -> None:
        """Embed should include hostname, service_name, and ip_address when present."""
        ticket = Ticket.objects.create(
            ticket_number="T001-099",
            team=self.team,
            category=TicketCategory.objects.get(pk=2),
            title="Reset Request",
            description="Please reset my box",
            status="open",
            hostname="webserver01",
            service_name="web:http",
            ip_address="10.0.0.42",
        )
        embed = format_ticket_embed(ticket)
        field_names = [field.name for field in embed.fields]
        field_values = [field.value for field in embed.fields]

        self.assertIn("Hostname", field_names)
        self.assertIn("Service", field_names)
        self.assertIn("IP Address", field_names)
        self.assertIn("webserver01", field_values)
        self.assertIn("web:http", field_values)
        self.assertIn("10.0.0.42", field_values)

    def test_format_ticket_embed_omits_empty_category_fields(self) -> None:
        """Embed should omit hostname/service/IP fields when not set."""
        ticket = Ticket.objects.create(
            ticket_number="T001-100",
            team=self.team,
            category=TicketCategory.objects.get(pk=6),
            title="General Question",
            description="Just a question",
            status="open",
        )
        embed = format_ticket_embed(ticket)
        field_names = [field.name for field in embed.fields]

        self.assertNotIn("Hostname", field_names)
        self.assertNotIn("Service", field_names)
        self.assertNotIn("IP Address", field_names)

    def test_format_ticket_embed_never_includes_playbook(self) -> None:
        """Team's Discord thread embed must never include category playbook steps."""
        cat, _ = TicketCategory.objects.get_or_create(
            pk=2,
            defaults={"display_name": "Box Reset", "points": 60},
        )
        cat.playbook = "1. Secret step one\n2. Secret step two"
        cat.save(update_fields=["playbook"])

        ticket = Ticket.objects.create(
            ticket_number="T001-101",
            team=self.team,
            category=cat,
            title="Reset Request",
            description="Please reset my box",
            status="open",
        )
        embed = format_ticket_embed(ticket)
        all_text = " ".join(
            [embed.title or "", embed.description or ""]
            + [f.name + " " + f.value for f in embed.fields]
            + [embed.footer.text if embed.footer else ""]
        )
        self.assertNotIn("Secret step one", all_text)
        self.assertNotIn("Secret step two", all_text)
        self.assertNotIn("Playbook", all_text)


class TestTriggerDashboard:
    def test_triggers_the_unified_dashboard(self) -> None:
        bot = MagicMock()

        trigger_dashboard(bot)

        bot.unified_dashboard.trigger_update.assert_called_once()

    def test_is_a_no_op_before_the_dashboard_starts(self) -> None:
        bot = MagicMock()
        bot.unified_dashboard = None

        trigger_dashboard(bot)
