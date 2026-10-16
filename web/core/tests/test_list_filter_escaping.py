"""A list page's filters are escaped where the table component echoes them, and a filter that isn't a number is
ignored rather than failing the page.

The table components got the filters as dynamic attributes they didn't declare, and django-cotton wrote them into
{{ attrs }} raw, so ?team=<script>...</script> ran as markup in the page."""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import example, given
from hypothesis import strategies as st

from core.tests.strategies import request_text, rolled_back

pytestmark = pytest.mark.django_db

ECHOED_FILTERS = [
    ("ticket_list", "team"),
    ("ticket_list", "search"),
    ("ops_review_tickets", "search"),
    ("scoring:red_team_scores", "attack_type"),
    ("scoring:red_team_findings", "submitter"),
    ("scoring:inject_grades_review", "inject"),
    ("scoring:review_incidents", "box"),
    ("scoring:review_orange", "search"),
]


@pytest.fixture
def admin_client(admin_user: User) -> Client:
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize(("name", "param"), ECHOED_FILTERS)
@given(markup=st.sampled_from(["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>', "<p data-probe=1>"]))
@example(markup="<script>alert(1)</script>")
def test_a_list_filter_is_escaped_where_the_page_echoes_it(
    admin_client: Client, name: str, param: str, markup: str
) -> None:
    with rolled_back():
        response = admin_client.get(reverse(name), {param: markup})
    assert response.status_code == 200
    assert markup not in response.content.decode()


NUMBER_FILTERS = [
    ("ops_review_tickets", "category"),
    ("scoring:red_team_findings", "team"),
    ("scoring:red_team_findings", "attack_type"),
    ("scoring:red_team_findings", "submitter"),
    ("scoring:red_team_scores", "team"),
    ("scoring:red_team_scores", "attack_type"),
    ("scoring:red_team_scores", "submitter"),
    ("scoring:review_incidents", "team"),
    ("scoring:inject_grades_review", "team"),
    ("scoring:review_orange", "team"),
    ("scoring:review_orange", "check"),
]


@pytest.mark.parametrize(("name", "param"), NUMBER_FILTERS)
@given(value=request_text(40).filter(lambda v: "\x00" not in v))
@example(value="pending")
@example(value=":")
def test_a_list_filter_that_is_not_a_number_is_ignored(admin_client: Client, name: str, param: str, value: str) -> None:
    with rolled_back():
        response = admin_client.get(reverse(name), {param: value})
    assert response.status_code == 200
