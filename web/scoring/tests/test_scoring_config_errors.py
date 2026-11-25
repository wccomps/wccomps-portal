"""The scoring setup page says why a save failed: the weights' sum (a form-wide error) and a field's own error. A
category weighted 0 is no error."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from scoring.models import ScoringTemplate

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


@pytest.mark.parametrize("weights", [("0", "60", "40"), ("60", "0", "40"), ("60", "40", "0")])
def test_a_zero_weight_saves_when_the_rest_make_100(client: Client, weights: tuple[str, str, str]) -> None:
    fields = dict(zip(("service_weight", "inject_weight", "orange_weight"), weights, strict=True))

    response = client.post(reverse("scoring:scoring_config"), fields)

    assert response.status_code == 302
    template = ScoringTemplate.objects.get()
    assert [template.service_weight, template.inject_weight, template.orange_weight] == [Decimal(w) for w in weights]
