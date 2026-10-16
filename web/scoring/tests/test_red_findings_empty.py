"""The findings table's empty state on both pages that show it: it says filters hide rows only when the page has
filters set, and its clear-filters link stays on the page."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

PAGES = ["scoring:red_team_scores", "scoring:red_team_findings"]


@pytest.fixture
def client(red_team_user: User) -> Client:
    client = Client()
    client.force_login(red_team_user)
    return client


@pytest.mark.parametrize("name", PAGES)
def test_with_no_filters_the_empty_state_does_not_blame_filters(client: Client, name: str) -> None:
    html = client.get(reverse(name)).content.decode()
    assert "match your filters" not in html


@pytest.mark.parametrize("name", PAGES)
def test_with_a_filter_set_the_clear_link_stays_on_the_page(client: Client, name: str) -> None:
    html = client.get(reverse(name), {"team": "5"}).content.decode()
    assert "match your filters" in html
    assert f'<a href="{reverse(name)}">clearing all filters</a>' in html
