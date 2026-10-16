"""A bulk Claim or Resolve with no tickets selected goes back to the list with a message, like the bulk approvals,
and a bulk action returns to the list as it was filtered."""

import pytest
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(ticketing_support_user: User) -> Client:
    client = Client()
    client.force_login(ticketing_support_user)
    return client


@pytest.mark.parametrize("name", ["tickets_bulk_claim", "tickets_bulk_resolve"])
@pytest.mark.parametrize("selection", ["", ",", " , "])
def test_an_empty_selection_returns_to_the_list_with_a_message(client: Client, name: str, selection: str) -> None:
    response = client.post(reverse(name), {"ticket_numbers": selection})
    assert response.status_code == 302
    assert response.url == reverse("ticket_list")
    assert [str(m) for m in get_messages(response.wsgi_request)] == ["No tickets selected"]


@pytest.mark.parametrize("name", ["tickets_bulk_claim", "tickets_bulk_resolve"])
@pytest.mark.parametrize(
    ("referer", "back"),
    [
        ("http://testserver/tickets/?status=open&search=a", "/tickets/?status=open&search=a"),
        ("http://testserver/tickets/T001-001/", "/tickets/"),
        ("https://elsewhere.example/tickets/?status=open", "/tickets/"),
    ],
)
def test_a_bulk_action_returns_to_the_filtered_list(client: Client, name: str, referer: str, back: str) -> None:
    response = client.post(reverse(name), {"ticket_numbers": "T999-001"}, headers={"referer": referer})
    assert response.status_code == 302
    assert response.url == back
