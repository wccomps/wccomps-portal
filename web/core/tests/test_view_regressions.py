"""Requests the view property tests (views_harness) found breaking a view, each pinned with @example of a failing
input, so each fix is held by a test of its own."""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import assume, example, given
from hypothesis import strategies as st

from core.tests.strategies import request_text, rolled_back

pytestmark = pytest.mark.django_db


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def admin_client(admin_user: User) -> Client:
    return signed_in(admin_user)


# ---------------------------------------------------------------------------------------------------------------
# A JSON endpoint answers a wrong method with a JSON error
# ---------------------------------------------------------------------------------------------------------------

JSON_POST_ENDPOINTS = [
    ("admin_broadcast_action", {}),
    ("admin_sync_roles_action", {}),
    ("admin_competition_action", {}),
    ("admin_teams_bulk_action", {}),
    ("admin_team_action", {"team_number": 1}),
]


@pytest.mark.parametrize(("name", "kwargs"), JSON_POST_ENDPOINTS)
@given(method=st.sampled_from(["GET", "PUT", "PATCH", "DELETE"]))
@example(method="GET")
def test_a_json_endpoint_answers_a_wrong_method_with_a_json_error(
    admin_client: Client, name: str, kwargs: dict[str, Any], method: str
) -> None:
    response = admin_client.generic(method, reverse(name, kwargs=kwargs), headers={"Sec-Fetch-Mode": "cors"})
    assert response.status_code == 405
    assert response["Content-Type"] == "application/json"
    assert response.json()["error"]


# ---------------------------------------------------------------------------------------------------------------
# A NUL character is refused before any view reads it
# ---------------------------------------------------------------------------------------------------------------

with_nul = st.tuples(request_text(40), request_text(40)).map(lambda t: f"{t[0]}\x00{t[1]}")


@given(text=with_nul)
@example(text="\x00")
def test_a_nul_character_in_a_path_is_refused(admin_client: Client, text: str) -> None:
    """PostgreSQL text can't hold NUL: the ticket lookup failed with a 500."""
    assume("/" not in text)
    with rolled_back():
        response = admin_client.get(reverse("ticket_detail", args=[text]))
    assert response.status_code == 400


@given(text=with_nul, fetch=st.booleans())
@example(text="\x00", fetch=False)
def test_a_nul_character_in_a_query_is_refused(admin_client: Client, text: str, fetch: bool) -> None:
    """The ticket review's search filtered on it and failed with a 500."""
    headers = {"Sec-Fetch-Mode": "cors"} if fetch else {}
    with rolled_back():
        response = admin_client.get(reverse("ops_review_tickets"), {"search": text}, headers=headers)
    assert response.status_code == 400
    if fetch:
        assert response.json()["error"]


@given(text=with_nul)
@example(text="\x00")
def test_a_nul_character_in_a_form_field_is_refused(admin_client: Client, text: str) -> None:
    with rolled_back():
        response = admin_client.post(reverse("scoring:bulk_approve_red_scores"), {"finding_ids": [text]})
    assert response.status_code == 400
