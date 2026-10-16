"""Requests the view property tests (views_harness) found breaking a view, each pinned with @example of a failing
input, so each fix is held by a test of its own."""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import example, given
from hypothesis import strategies as st

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
