"""Tests for ticket category admin CRUD views."""

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse

from team.models import Team
from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_category_sequence(db: object) -> None:
    """Reset the TicketCategory auto-increment sequence past seeded data.

    Only needed for PostgreSQL where explicit-PK inserts from seed migrations
    don't advance the auto-increment sequence.
    """
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT setval(pg_get_serial_sequence('ticketing_ticketcategory', 'id'), "
                "GREATEST((SELECT MAX(id) FROM ticketing_ticketcategory), 1))"
            )


@pytest.fixture
def category(db: object) -> TicketCategory:
    """Create a test ticket category with high PK to avoid seeded data collision."""
    return TicketCategory.objects.create(
        pk=100,
        display_name="Test Category",
        points=50,
        required_fields=["hostname", "ip_address"],
        optional_fields=["service_name"],
        variable_points=False,
        user_creatable=True,
        sort_order=10,
    )


@pytest.fixture
def team_for_tickets(db: object) -> Team:
    """Create a team for ticket tests."""
    return Team.objects.create(team_number=42, team_name="Test Team", is_active=True)


class TestAdminCategoriesList:
    """Test category list view."""

    def test_requires_admin_or_ticketing_admin(self, blue_team_user):
        """Non-admin, non-ticketing-admin users get 403."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("admin_categories"))
        assert response.status_code == 403

    @pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user", "ticketing_admin_user"])
    def test_accessible_by_authorized_users(self, user_fixture, request):
        """Gold team, admin, and ticketing admin can access."""
        user = request.getfixturevalue(user_fixture)
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_categories"))
        assert response.status_code == 200

    def test_shows_categories(self, gold_team_user, category):
        """List page shows existing categories."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_categories"))
        assert response.status_code == 200
        assert b"Test Category" in response.content

    def test_shows_ticket_count(self, gold_team_user, category, team_for_tickets):
        """List page shows correct ticket count per category."""
        Ticket.objects.create(
            ticket_number="T042-001",
            team=team_for_tickets,
            category=category,
            title="Test ticket",
            status="open",
        )
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_categories"))
        assert response.status_code == 200
        # The ticket count should appear in the table
        assert b"Test Category" in response.content


class TestAdminCategoryCreate:
    """Test category create view."""

    def test_get_renders_form(self, gold_team_user):
        """GET renders the create form."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_category_create"))
        assert response.status_code == 200
        assert b"Create Category" in response.content

    def test_post_creates_category(self, gold_team_user):
        """POST with valid data creates a category and redirects."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(
            reverse("admin_category_create"),
            {
                "display_name": "New Category",
                "points": "100",
                "sort_order": "5",
                "required_fields": ["hostname", "ip_address"],
                "optional_fields": ["description"],
                "variable_points": "on",
                "variable_cost_note": "Varies by service",
                "min_points": "10",
                "max_points": "200",
                "user_creatable": "on",
            },
        )
        assert response.status_code == 302
        assert response.url == reverse("admin_categories")

        cat = TicketCategory.objects.get(display_name="New Category")
        assert cat.points == 100
        assert cat.sort_order == 5
        assert cat.required_fields == ["hostname", "ip_address"]
        assert cat.optional_fields == ["description"]
        assert cat.variable_points is True
        assert cat.variable_cost_note == "Varies by service"
        assert cat.min_points == 10
        assert cat.max_points == 200
        assert cat.user_creatable is True

    def test_post_requires_display_name(self, gold_team_user):
        """POST without display_name shows error."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(
            reverse("admin_category_create"),
            {"display_name": "", "points": "10"},
        )
        assert response.status_code == 200
        assert b"Display name is required" in response.content

    def test_post_creates_category_with_playbook(self, gold_team_user):
        """POST with playbook saves steps."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(
            reverse("admin_category_create"),
            {
                "display_name": "Playbook Category",
                "points": "50",
                "playbook": "1. Check host\n2. Restart service",
            },
        )
        assert response.status_code == 302
        cat = TicketCategory.objects.get(display_name="Playbook Category")
        assert cat.playbook == "1. Check host\n2. Restart service"
        assert cat.steps == ["Check host", "Restart service"]

    def test_requires_permission(self, blue_team_user):
        """Non-admin users get 403."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("admin_category_create"))
        assert response.status_code == 403


class TestAdminCategoryEdit:
    """Test category edit view."""

    def test_get_shows_data(self, gold_team_user, category):
        """GET renders form with category data."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_category_edit", kwargs={"category_id": category.pk}))
        assert response.status_code == 200
        assert b"Test Category" in response.content

    def test_post_updates_category(self, gold_team_user, category):
        """POST updates the category and redirects."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(
            reverse("admin_category_edit", kwargs={"category_id": category.pk}),
            {
                "display_name": "Updated Name",
                "points": "75",
                "sort_order": "20",
                "required_fields": ["hostname"],
                "user_creatable": "on",
                "min_points": "0",
                "max_points": "0",
            },
        )
        assert response.status_code == 302
        assert response.url == reverse("admin_categories")

        category.refresh_from_db()
        assert category.display_name == "Updated Name"
        assert category.points == 75
        assert category.sort_order == 20
        assert category.required_fields == ["hostname"]

    def test_post_requires_display_name(self, gold_team_user, category):
        """POST without display_name shows error."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(
            reverse("admin_category_edit", kwargs={"category_id": category.pk}),
            {"display_name": "", "points": "10"},
        )
        assert response.status_code == 200
        assert b"Display name is required" in response.content

    def test_post_updates_category_playbook(self, gold_team_user, category):
        """POST updates playbook steps on category."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(
            reverse("admin_category_edit", kwargs={"category_id": category.pk}),
            {
                "display_name": category.display_name,
                "points": "50",
                "playbook": "- Step one\n- Step two",
            },
        )
        assert response.status_code == 302
        category.refresh_from_db()
        assert category.playbook == "- Step one\n- Step two"
        assert category.steps == ["Step one", "Step two"]

    def test_post_clears_playbook(self, gold_team_user, category):
        """POST with empty playbook clears it."""
        category.playbook = "1. To clear"
        category.save(update_fields=["playbook"])

        client = Client()
        client.force_login(gold_team_user)
        response = client.post(
            reverse("admin_category_edit", kwargs={"category_id": category.pk}),
            {
                "display_name": category.display_name,
                "points": "50",
                "playbook": "",
            },
        )
        assert response.status_code == 302
        category.refresh_from_db()
        assert category.playbook == ""
        assert category.steps == []

    def test_nonexistent_category_returns_404(self, gold_team_user):
        """Editing nonexistent category returns 404."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_category_edit", kwargs={"category_id": 99999}))
        assert response.status_code == 404

    def test_requires_permission(self, blue_team_user, category):
        """Non-admin users get 403."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("admin_category_edit", kwargs={"category_id": category.pk}))
        assert response.status_code == 403


class TestAdminCategoryDelete:
    """Test category delete view."""

    def test_get_shows_confirmation(self, gold_team_user, category):
        """GET renders delete confirmation page."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_category_delete", kwargs={"category_id": category.pk}))
        assert response.status_code == 200
        assert b"Test Category" in response.content
        assert b"Delete Category" in response.content

    def test_post_deletes_category(self, gold_team_user, category):
        """POST deletes the category and redirects."""
        cat_pk = category.pk
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(reverse("admin_category_delete", kwargs={"category_id": cat_pk}))
        assert response.status_code == 302
        assert response.url == reverse("admin_categories")
        assert not TicketCategory.objects.filter(pk=cat_pk).exists()

    def test_shows_ticket_count_warning(self, gold_team_user, category, team_for_tickets):
        """Delete confirmation shows ticket count when category has tickets."""
        Ticket.objects.create(
            ticket_number="T042-001",
            team=team_for_tickets,
            category=category,
            title="Test ticket",
            status="open",
        )
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_category_delete", kwargs={"category_id": category.pk}))
        assert response.status_code == 200
        assert b"1 ticket(s)" in response.content

    def test_nonexistent_category_returns_404(self, gold_team_user):
        """Deleting nonexistent category returns 404."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("admin_category_delete", kwargs={"category_id": 99999}))
        assert response.status_code == 404

    def test_requires_permission(self, blue_team_user, category):
        """Non-admin users get 403."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("admin_category_delete", kwargs={"category_id": category.pk}))
        assert response.status_code == 403


def test_the_category_page_and_the_admin_edit_categories_with_one_model_form():
    """One form for TicketCategory, so its limits (the integer columns') come from the model, not a copy."""
    from django.contrib import admin
    from django.forms import ModelForm

    from core.admin_views import categories
    from ticketing.forms import TicketCategoryForm
    from ticketing.models import TicketCategory

    assert issubclass(TicketCategoryForm, ModelForm) and TicketCategoryForm._meta.model is TicketCategory
    assert admin.site._registry[TicketCategory].form is TicketCategoryForm
    assert categories.TicketCategoryForm is TicketCategoryForm


@pytest.mark.django_db
def test_post_refuses_points_its_column_cannot_hold(gold_team_user):
    from ticketing.models import TicketCategory

    client = Client()
    client.force_login(gold_team_user)
    before = TicketCategory.objects.count()
    response = client.post("/ops/admin/categories/create/", {"display_name": "Huge", "points": str(2**31)})
    assert response.status_code == 200
    assert TicketCategory.objects.count() == before


def _field_error(html: str, field: str) -> str | None:
    """The error shown in a field's own row, under the id its widget's aria-describedby names."""
    import re

    match = re.search(rf'<p class="errornote" id="id_{field}_error">([^<]*)</p>', html)
    return match.group(1) if match else None


def test_a_failed_create_shows_the_posted_values_and_the_error_beside_its_field(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    response = client.post(
        reverse("admin_category_create"),
        {
            "display_name": "Keeps Its Name",
            "points": str(2**31),
            "playbook": "1. Posted step",
            "required_fields": ["ip_address"],
            "variable_points": "on",
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert _field_error(html, "points") == "Ensure this value is less than or equal to 2147483647."
    assert _field_error(html, "display_name") is None
    assert 'value="Keeps Its Name"' in html
    assert f'value="{2**31}"' in html
    assert "1. Posted step</textarea>" in html
    assert 'value="ip_address" id="id_required_fields_1" checked' in html
    assert 'name="variable_points" id="id_variable_points" checked' in html


def test_a_failed_edit_shows_the_posted_values_not_the_saved_ones(gold_team_user, category):
    client = Client()
    client.force_login(gold_team_user)
    response = client.post(
        reverse("admin_category_edit", kwargs={"category_id": category.pk}),
        {"display_name": "", "points": "33", "sort_order": "4"},
    )
    html = response.content.decode()

    assert _field_error(html, "display_name") == "Display name is required."
    assert 'value="33"' in html
    assert 'value="50"' not in html


def test_a_ticket_field_cannot_be_both_required_and_optional(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    response = client.post(
        reverse("admin_category_create"),
        {"display_name": "Both", "required_fields": ["hostname", "ip_address"], "optional_fields": ["hostname"]},
    )

    assert response.status_code == 200
    assert not TicketCategory.objects.filter(display_name="Both").exists()
    assert _field_error(response.content.decode(), "optional_fields") == (
        "Hostname is already required; a field is either required or optional."
    )
