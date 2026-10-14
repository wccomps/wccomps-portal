"""A form field shows an error box only when it has an error, however the template wraps the error attribute."""

import pytest
from django.test import Client
from django.urls import reverse

from registration.models import Season

pytestmark = pytest.mark.django_db


@pytest.fixture
def gold_client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _page(client, season):
    return client.get(reverse("registration_season_edit", args=[season.pk])).content.decode()


def test_no_error_box_without_an_error(gold_client):
    season = Season.objects.create(name="Next", year=2093)

    assert 'class="errornote"' not in _page(gold_client, season)


def test_a_real_error_still_shows(gold_client):
    season = Season.objects.create(name="Next", year=2093)
    Season.objects.create(name="Other", year=2094)

    response = gold_client.post(reverse("registration_season_edit", args=[season.pk]), {"name": "Next", "year": 2094})

    assert response.content.decode().count('class="errornote"') == 1
