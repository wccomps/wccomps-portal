"""Teams create tickets only in user-creatable categories; staff may use any."""

import json
import re
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from ticketing.models import Ticket, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quotient_never_asked():
    """The form reads the synced snapshot; a live Quotient call would hang it while Quotient is down."""
    with patch("quotient.client.get_quotient_client", side_effect=AssertionError("the ticket form asked Quotient")):
        yield


@pytest.fixture
def staff_only():
    return TicketCategory.objects.create(
        pk=95, display_name="Staff Only", points=0, required_fields=[], user_creatable=False, sort_order=95
    )


@pytest.fixture
def team_1(blue_team):
    return blue_team


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def test_team_form_hides_staff_only_category(blue_team_user, team_1, staff_only):
    html = _client(blue_team_user).get(reverse("create_ticket")).content.decode()
    assert "Staff Only" not in html


def test_the_page_gives_its_script_each_categorys_required_and_optional_fields(blue_team_user, team_1, staff_only):
    web = TicketCategory.objects.create(
        pk=96,
        display_name="Web",
        points=0,
        required_fields=["hostname"],
        optional_fields=["ip_address"],
        user_creatable=True,
        sort_order=96,
    )
    html = _client(blue_team_user).get(reverse("create_ticket")).content.decode()

    script = re.search(r'<script id="categories-data" type="application/json">(.*?)</script>', html, re.DOTALL)
    assert script is not None
    categories = json.loads(script.group(1))
    assert categories[str(web.pk)] == {"required_fields": ["hostname"], "optional_fields": ["ip_address"]}
    assert str(staff_only.pk) not in categories


def test_team_cannot_post_staff_only_category(blue_team_user, team_1, staff_only):
    response = _client(blue_team_user).post(
        reverse("create_ticket"), {"title": "x", "category": staff_only.pk, "description": "x"}
    )
    assert "Invalid ticket category selected." in response.content.decode()
    assert not Ticket.objects.exists()


def test_staff_can_use_staff_only_category(ticketing_admin_user, team_1, staff_only):
    response = _client(ticketing_admin_user).post(
        reverse("create_ticket"), {"title": "x", "category": staff_only.pk, "description": "x", "team_id": team_1.id}
    )
    assert response.status_code == 302
    assert Ticket.objects.get().category == staff_only


def test_box_reset_keeps_the_teams_description_and_the_hostname(blue_team_user, team_1, box_reset_category):
    response = _client(blue_team_user).post(
        reverse("create_ticket"),
        {
            "title": "Web box won't boot",
            "category": box_reset_category.pk,
            "description": "Stuck at the boot loader since the last round",
            "hostname": "web01",
            "ip_address": "10.0.1.5",
        },
    )

    assert response.status_code == 302
    ticket = Ticket.objects.get()
    assert ticket.description == "Stuck at the boot loader since the last round"
    assert ticket.hostname == "web01"


def test_known_hostname_satisfies_required_ip_and_unknown_does_not(blue_team_user, team_1, box_reset_category):
    from scoring.models import QuotientMetadataCache

    QuotientMetadataCache.objects.create(
        boxes=[
            {"name": "web01", "ip": "10.0.1.5", "services": []},
            {"name": "router", "ip": "", "services": []},
        ]
    )

    unknown = _client(blue_team_user).post(
        reverse("create_ticket"),
        {"title": "Reset", "category": box_reset_category.pk, "hostname": "unknown01"},
    )
    assert "IP Address is required for this category." in unknown.content.decode()
    assert not Ticket.objects.exists()

    known = _client(blue_team_user).post(
        reverse("create_ticket"),
        {"title": "Reset", "category": box_reset_category.pk, "hostname": "web01"},
    )
    assert known.status_code == 302
    ticket = Ticket.objects.get(hostname="web01")
    assert ticket.ip_address == "10.0.1.5"

    unscored = _client(blue_team_user).post(
        reverse("create_ticket"),
        {"title": "Reset router", "category": box_reset_category.pk, "hostname": "router"},
    )
    assert unscored.status_code == 302
    router_ticket = Ticket.objects.get(hostname="router")
    assert router_ticket.ip_address is None


@pytest.fixture
def service_category(db):
    from scoring.models import QuotientMetadataCache

    from core.models import CompetitionConfig

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})
    QuotientMetadataCache.objects.create(
        boxes=[
            {"name": "db01", "ip": "10.0.1.6", "services": [{"name": "MySQL", "display_name": "MySQL"}]},
            {"name": "web01", "ip": "10.0.1.5", "services": [{"name": "HTTP", "display_name": "HTTP"}]},
        ]
    )
    return TicketCategory.objects.create(
        pk=97,
        display_name="Service Down",
        points=0,
        required_fields=["hostname", "service_name"],
        optional_fields=["ip_address"],
        user_creatable=True,
        sort_order=97,
    )


def test_a_service_picked_without_a_box_files_the_ticket_on_its_box(blue_team_user, team_1, service_category):
    response = _client(blue_team_user).post(
        reverse("create_ticket"),
        {"title": "Site down", "category": service_category.pk, "service_name": "web01:HTTP"},
    )

    assert response.status_code == 302
    ticket = Ticket.objects.get()
    assert (ticket.hostname, ticket.ip_address, ticket.service_name) == ("web01", "10.0.1.5", "web01:HTTP")


def test_a_box_picked_with_a_service_is_kept(blue_team_user, team_1, service_category):
    response = _client(blue_team_user).post(
        reverse("create_ticket"),
        {"title": "Site down", "category": service_category.pk, "service_name": "web01:HTTP", "hostname": "db01"},
    )

    assert response.status_code == 302
    ticket = Ticket.objects.get()
    assert (ticket.hostname, ticket.ip_address) == ("db01", "10.0.1.6")


def test_a_service_quotient_does_not_know_names_no_box(blue_team_user, team_1, service_category):
    response = _client(blue_team_user).post(
        reverse("create_ticket"),
        {"title": "Site down", "category": service_category.pk, "service_name": "mail01:SMTP"},
    )

    assert "Hostname is required for this category." in response.content.decode()
    assert not Ticket.objects.exists()


def test_a_known_box_excuses_only_the_ip_not_the_fields_after_it(blue_team_user, team_1):
    from scoring.models import QuotientMetadataCache

    QuotientMetadataCache.objects.create(boxes=[{"name": "web01", "ip": "10.0.1.5", "services": []}])
    category = TicketCategory.objects.create(
        pk=98,
        display_name="Box Down",
        points=0,
        required_fields=["hostname", "ip_address", "description"],
        user_creatable=True,
        sort_order=98,
    )

    response = _client(blue_team_user).post(
        reverse("create_ticket"), {"title": "Box down", "category": category.pk, "hostname": "web01"}
    )

    assert "Description is required for this category." in response.content.decode()
    assert not Ticket.objects.exists()


def test_the_form_offers_only_team_categories_unless_told_it_is_for_staff(staff_only):
    from ticketing.forms import CreateTicketForm

    form = CreateTicketForm({"title": "x", "category": staff_only.pk, "description": "x"})

    assert form.errors["category"] == ["Invalid ticket category selected."]
