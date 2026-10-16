"""A bulk Claim or Resolve with no tickets selected goes back to the list with a message, like the bulk approvals."""

import pytest
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("name", ["tickets_bulk_claim", "tickets_bulk_resolve"])
def test_an_empty_selection_returns_to_the_list_with_a_message(ticketing_support_user: User, name: str) -> None:
    client = Client()
    client.force_login(ticketing_support_user)
    response = client.post(reverse(name), {"ticket_numbers": ""})
    assert response.status_code == 302
    assert response.url == reverse("ticket_list")
    assert [str(m) for m in get_messages(response.wsgi_request)] == ["No tickets selected"]
