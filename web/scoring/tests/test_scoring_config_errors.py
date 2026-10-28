"""The scoring setup page says why a save failed: the weights' sum (a form-wide error) and a field's own error."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user: User) -> Client:
    client = Client()
    client.force_login(gold_team_user)
    return client


def test_weights_not_summing_to_100_say_so(client: Client) -> None:
    response = client.post(
        reverse("scoring:scoring_config"), {"service_weight": "50", "inject_weight": "30", "orange_weight": "10"}
    )
    assert response.status_code == 200
    assert "Weights must sum to 100 (currently 90" in response.content.decode()


def test_a_bad_weight_shows_its_own_error(client: Client) -> None:
    response = client.post(
        reverse("scoring:scoring_config"), {"service_weight": "lots", "inject_weight": "30", "orange_weight": "10"}
    )
    assert response.status_code == 200
    assert "Enter a number." in response.content.decode()
