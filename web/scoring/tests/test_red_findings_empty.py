"""The findings table on both pages that show it: the empty state says filters hide rows only when the page has
filters set and its clear-filters link stays on the page, and a finding without an attack type names its vector,
or Unknown."""

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


@pytest.mark.parametrize(("vector", "shown"), [("", "Unknown"), ("ssh brute force", "ssh brute force")])
def test_a_finding_without_an_attack_type_shows_its_vector_or_unknown(
    client: Client, red_team_user: User, vector: str, shown: str
) -> None:
    from decimal import Decimal

    from scoring.models import RedTeamScore

    RedTeamScore.objects.create(attack_vector=vector, points_per_team=Decimal(10), submitted_by=red_team_user)
    html = client.get(reverse("scoring:red_team_scores")).content.decode()
    assert f"<td>{shown}</td>" in html
