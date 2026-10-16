"""Tests for Django admin ticketing interface.

Covers: ticket creation via admin (auto-generated ticket numbers),
readonly fields, CSV export, and CommentRateLimit permissions.
"""

import csv
import io

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory

from team.models import Team
from ticketing.admin import CommentRateLimitAdmin, TicketAdmin
from ticketing.models import CommentRateLimit, Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def team(db: object) -> Team:
    """Create a test team."""
    return Team.objects.create(team_number=5, team_name="Test Team", is_active=True)


@pytest.fixture
def ticket_admin() -> TicketAdmin:
    """Create a TicketAdmin instance."""
    return TicketAdmin(Ticket, AdminSite())


@pytest.fixture
def ticket(team: Team) -> Ticket:
    """Create a test ticket."""
    return Ticket.objects.create(
        ticket_number="T005-001",
        team=team,
        category=TicketCategory.objects.get(pk=6),
        title="Test ticket",
        status="open",
    )


class TestTicketAdminCreate:
    """Test creating tickets via Django admin."""

    def test_auto_generates_ticket_number(self, ticket_admin: TicketAdmin, admin_user: User, team: Team) -> None:
        """Creating a ticket without a ticket_number auto-generates one."""
        request = RequestFactory().post("/admin/ticketing/ticket/add/")
        request.user = admin_user

        obj = Ticket(team=team, category=TicketCategory.objects.get(pk=6), title="Admin ticket")
        ticket_admin.save_model(request, obj, form=None, change=False)

        assert obj.pk is not None
        assert obj.ticket_number == "T005-001"

    def test_auto_generate_increments_counter(self, ticket_admin: TicketAdmin, admin_user: User, team: Team) -> None:
        """Each auto-generated ticket increments the team counter."""
        request = RequestFactory().post("/admin/ticketing/ticket/add/")
        request.user = admin_user

        obj1 = Ticket(team=team, category=TicketCategory.objects.get(pk=6), title="First")
        ticket_admin.save_model(request, obj1, form=None, change=False)

        obj2 = Ticket(team=team, category=TicketCategory.objects.get(pk=6), title="Second")
        ticket_admin.save_model(request, obj2, form=None, change=False)

        assert obj1.ticket_number == "T005-001"
        assert obj2.ticket_number == "T005-002"
        team.refresh_from_db()
        assert team.ticket_counter == 2

    def test_manual_ticket_number_preserved(self, ticket_admin: TicketAdmin, admin_user: User, team: Team) -> None:
        """Providing a ticket_number manually should not auto-generate."""
        request = RequestFactory().post("/admin/ticketing/ticket/add/")
        request.user = admin_user

        obj = Ticket(
            team=team,
            category=TicketCategory.objects.get(pk=6),
            title="Manual",
            ticket_number="CUSTOM-001",
        )
        ticket_admin.save_model(request, obj, form=None, change=False)

        assert obj.ticket_number == "CUSTOM-001"
        team.refresh_from_db()
        assert team.ticket_counter == 0  # counter not touched

    def test_save_existing_ticket_does_not_regenerate(
        self, ticket_admin: TicketAdmin, admin_user: User, ticket: Ticket
    ) -> None:
        """Saving an existing ticket should not change its ticket_number."""
        request = RequestFactory().post("/admin/ticketing/ticket/1/change/")
        request.user = admin_user

        original_number = ticket.ticket_number
        ticket.title = "Updated title"
        ticket_admin.save_model(request, ticket, form=None, change=True)

        ticket.refresh_from_db()
        assert ticket.ticket_number == original_number
        assert ticket.title == "Updated title"


class TestTicketAdminReadonlyFields:
    """Test readonly field behavior on add vs change."""

    def test_ticket_number_editable_on_add(self, ticket_admin: TicketAdmin, admin_user: User) -> None:
        """ticket_number should be editable when adding a new ticket."""
        request = RequestFactory().get("/admin/ticketing/ticket/add/")
        request.user = admin_user

        readonly = ticket_admin.get_readonly_fields(request, obj=None)
        assert "ticket_number" not in readonly

    def test_ticket_number_readonly_on_change(
        self, ticket_admin: TicketAdmin, admin_user: User, ticket: Ticket
    ) -> None:
        """ticket_number should be read-only when editing an existing ticket."""
        request = RequestFactory().get("/admin/ticketing/ticket/1/change/")
        request.user = admin_user

        readonly = ticket_admin.get_readonly_fields(request, obj=ticket)
        assert "ticket_number" in readonly

    def test_audit_fields_always_readonly(self, ticket_admin: TicketAdmin, admin_user: User, ticket: Ticket) -> None:
        """created_at and updated_at should always be read-only."""
        request = RequestFactory().get("/admin/ticketing/ticket/add/")
        request.user = admin_user

        readonly_add = ticket_admin.get_readonly_fields(request, obj=None)
        readonly_change = ticket_admin.get_readonly_fields(request, obj=ticket)

        assert "created_at" in readonly_add
        assert "updated_at" in readonly_add
        assert "created_at" in readonly_change
        assert "updated_at" in readonly_change


class TestTicketAdminExportCSV:
    """Test CSV export action."""

    def test_export_produces_valid_csv(self, ticket_admin: TicketAdmin, admin_user: User, ticket: Ticket) -> None:
        """Export action should produce valid CSV with correct headers and data."""
        request = RequestFactory().post("/admin/ticketing/ticket/")
        request.user = admin_user

        response = ticket_admin.export_as_csv(request, Ticket.objects.all())

        assert response["Content-Type"] == "text/csv"
        assert "tickets.csv" in response["Content-Disposition"]

        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)

        # Header row
        assert rows[0][0] == "Ticket Number"
        assert len(rows[0]) == 16

        # Data row
        assert rows[1][0] == "T005-001"
        assert rows[1][1] == "Test Team"
        assert rows[1][2] == "5"

    def test_export_handles_unresolved_ticket(
        self, ticket_admin: TicketAdmin, admin_user: User, ticket: Ticket
    ) -> None:
        """Export should handle tickets with no resolved_at gracefully."""
        request = RequestFactory().post("/admin/ticketing/ticket/")
        request.user = admin_user

        response = ticket_admin.export_as_csv(request, Ticket.objects.all())
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)

        # Resolved At column should be empty string
        assert rows[1][13] == ""

    def test_export_empty_queryset(self, ticket_admin: TicketAdmin, admin_user: User, db: object) -> None:
        """Export with no tickets should produce only headers."""
        request = RequestFactory().post("/admin/ticketing/ticket/")
        request.user = admin_user

        response = ticket_admin.export_as_csv(request, Ticket.objects.none())
        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)

        assert len(rows) == 1  # header only


class TestCommentRateLimitAdmin:
    """Test CommentRateLimit admin restrictions."""

    def test_add_permission_denied(self, admin_user: User) -> None:
        """Manual creation of rate limit entries should be blocked."""
        rate_limit_admin = CommentRateLimitAdmin(CommentRateLimit, AdminSite())
        request = RequestFactory().get("/admin/ticketing/commentratelimit/add/")
        request.user = admin_user

        assert rate_limit_admin.has_add_permission(request) is False


def test_audit_log_is_read_only_in_admin():
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from core.admin import AuditLogAdmin
    from core.models import AuditLog

    model_admin = AuditLogAdmin(AuditLog, AdminSite())
    request = RequestFactory().get("/")
    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
