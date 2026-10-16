"""The findings table on both pages that show it: the empty state says filters hide rows only when the page has
filters set and its clear-filters link stays on the page, and a finding without an attack type names its vector,
or Unknown. A finding's own page says Unknown for the attack type and shows a legacy finding's vector in a row of
its own."""

import re

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
    assert re.search(rf'<a href="{re.escape(reverse(name))}"[^>]*>\s*clearing all filters\s*</a>', html)


@pytest.mark.parametrize(("vector", "shown"), [("", "Unknown"), ("ssh brute force", "ssh brute force")])
def test_a_finding_without_an_attack_type_shows_its_vector_or_unknown(
    client: Client, red_team_user: User, vector: str, shown: str
) -> None:
    from decimal import Decimal

    from scoring.models import RedTeamScore

    RedTeamScore.objects.create(attack_vector=vector, points_per_team=Decimal(10), submitted_by=red_team_user)
    html = client.get(reverse("scoring:red_team_scores")).content.decode()
    assert f"<td>{shown}</td>" in html


def test_a_finding_page_without_an_attack_type_says_unknown_and_shows_its_vector(
    client: Client, red_team_user: User
) -> None:
    from decimal import Decimal

    from scoring.models import RedTeamScore

    finding = RedTeamScore.objects.create(
        attack_vector="a long description of the attack", points_per_team=Decimal(10), submitted_by=red_team_user
    )
    html = client.get(reverse("scoring:view_red_score", args=[finding.pk])).content.decode()
    attack_type_row = html.split("Attack Type", 1)[1].split("</dd>", 1)[0]
    assert "Unknown" in attack_type_row
    vector_row = html.split("Attack Vector", 1)[1].split("</dd>", 1)[0]
    assert "a long description of the attack" in vector_row
