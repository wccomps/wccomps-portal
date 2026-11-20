"""The submit and ticket forms read box metadata from the synced cache, never from Quotient during the request."""

from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from scoring.models import QuotientMetadataCache
from scoring.quotient_sync import get_service_choices, ticket_infrastructure

# Metadata as sync_quotient_metadata stores it; ROUTER is an unscored box merged from Proxmox
BOXES = [
    {
        "name": "WEB",
        "ip": "10.0.0.5",
        "services": [
            {"name": "http", "display_name": "HTTP"},
            {"name": "api", "display_name": "API"},
            {"name": "Other", "display_name": "Something else"},
        ],
    },
    {"name": "ROUTER", "ip": "", "services": []},
]

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("url_name", "user_fixture"),
    [("scoring:submit_incident_report", "gold_team_user"), ("scoring:submit_red_score", "red_team_user")],
)
def test_submit_page_uses_cached_metadata_without_calling_quotient(request, url_name, user_fixture):
    QuotientMetadataCache.objects.create(
        boxes=[{"name": "web01", "ip": "10.0.0.5", "services": [{"name": "http"}, {"name": "ssh"}]}]
    )
    client = Client()
    client.force_login(request.getfixturevalue(user_fixture))

    with patch("quotient.client.QuotientClient._request") as quotient_request:
        response = client.get(reverse(url_name))

    assert response.status_code == 200
    quotient_request.assert_not_called()
    assert response.context["box_metadata"] == {"web01": {"ip": "10.0.0.5", "services": ["http", "ssh"]}}


def test_service_choices_are_named_once_by_display_name_with_other_last() -> None:
    assert get_service_choices() == [("Other", "Other")]

    QuotientMetadataCache.objects.create(
        boxes=BOXES,
        services=[
            {"name": "ssh", "display_name": ""},
            {"name": "http", "display_name": "HTTP"},
            {"name": "", "display_name": "Unnamed"},
        ],
    )

    assert get_service_choices() == [("http", "HTTP"), ("api", "API"), ("ssh", "ssh"), ("Other", "Other")]


def test_ticket_services_carry_their_box_and_sort_by_label() -> None:
    QuotientMetadataCache.objects.create(boxes=BOXES)

    box_names, box_ips, services = ticket_infrastructure(staff=True)

    assert (box_names, box_ips) == (["ROUTER", "WEB"], {"WEB": "10.0.0.5", "ROUTER": ""})
    assert [(s["value"], s["label"], s["box_ip"], s["box_name"], s["service_name"]) for s in services] == [
        ("WEB:api", "WEB: API", "10.0.0.5", "WEB", "api"),
        ("WEB:http", "WEB: HTTP", "10.0.0.5", "WEB", "http"),
        ("WEB:Other", "WEB: Something else", "10.0.0.5", "WEB", "Other"),
    ]


def test_ticket_services_wait_for_applications_unless_the_caller_says_staff() -> None:
    from core.models import CompetitionConfig

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    QuotientMetadataCache.objects.create(boxes=BOXES)

    assert ticket_infrastructure()[2] == []
    assert len(ticket_infrastructure(staff=True)[2]) == 3
