"""Requests the view property tests (views_harness) found breaking a view, each pinned with @example of a failing
input, so each fix is held by a test of its own."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import assume, example, given
from hypothesis import strategies as st
from orange_team.models import OrangeAssignment, OrangeAssignmentResult, OrangeCheck, OrangeCheckCriterion

from core.tests.strategies import request_text, rolled_back
from team.models import DiscordLink, Team
from ticketing.models import Ticket

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


# ---------------------------------------------------------------------------------------------------------------
# A refusal or a missing row is answered with its status, not a 200 error page
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def tickets(blue_team_user: User, default_category: Any) -> dict[str, Any]:
    own = Team.objects.create(team_number=1, team_name="Team 01")
    other = Team.objects.create(team_number=2, team_name="Team 02")
    DiscordLink.objects.filter(user=blue_team_user).update(team=own)
    return {
        "own": Ticket.objects.create(team=own, category=default_category, ticket_number="T001-001", title="Ours"),
        "other": Ticket.objects.create(team=other, category=default_category, ticket_number="T002-001", title="X"),
    }


@given(number=request_text(20).filter(lambda n: n and "/" not in n and "\x00" not in n))
@example(number="0")
def test_a_missing_ticket_is_a_404(admin_client: Client, tickets: dict[str, Any], number: str) -> None:
    assume(number not in ("T001-001", "T002-001"))
    with rolled_back():
        response = admin_client.get(reverse("ticket_detail", args=[number]))
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("role", "method", "url", "status"),
    [
        ("support", "get", lambda t: reverse("create_ticket"), 403),
        ("admin", "post", lambda t: reverse("ticket_cancel", args=[t["own"].ticket_number]), 403),
        ("blue", "post", lambda t: reverse("ticket_cancel", args=[t["other"].ticket_number]), 404),
        ("admin", "get", lambda t: reverse("school_info_edit", args=[99]), 404),
        ("admin", "get", lambda t: reverse("admin_team_detail", args=[99]), 404),
    ],
)
def test_a_refusal_page_carries_its_status(
    request: pytest.FixtureRequest,
    tickets: dict[str, Any],
    role: str,
    method: str,
    url: Callable[[dict[str, Any]], str],
    status: int,
) -> None:
    """These pages said "Access denied" or "not found" with a 200, so nothing could tell them from a success."""
    users = {"support": "ticketing_support_user", "admin": "admin_user", "blue": "blue_team_user"}
    client = signed_in(request.getfixturevalue(users[role]))
    response = getattr(client, method)(url(tickets))
    assert response.status_code == status
    tickets["own"].refresh_from_db()
    assert tickets["own"].status == "open"


# ---------------------------------------------------------------------------------------------------------------
# The rubric autosave answers any body with JSON
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def rubric(orange_team_user: User) -> dict[str, Any]:
    check = OrangeCheck.objects.create(title="Phones", description="", max_points=5)
    criterion = OrangeCheckCriterion.objects.create(orange_check=check, label="Answered", points=5, sort_order=1)
    team = Team.objects.create(team_number=1, team_name="Team 01")
    assignment = OrangeAssignment.objects.create(orange_check=check, user=orange_team_user, team=team)
    OrangeAssignmentResult.objects.create(assignment=assignment, criterion=criterion)
    return {"client": signed_in(orange_team_user), "assignment": assignment, "criterion": criterion}


json_values = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text(max_size=10)),
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=8), inner, max_size=3)),
    max_leaves=8,
)


@given(
    body=st.one_of(
        st.binary(max_size=50),
        json_values.map(lambda v: json.dumps(v).encode()),
        st.fixed_dictionaries({}, optional={"criterion_id": json_values, "met": json_values}).map(
            lambda v: json.dumps(v).encode()
        ),
    )
)
@example(body=b"[]")
@example(body=b"\xb8")
@example(body=b'{"criterion_id": "x", "met": true}')
@example(body=b'{"criterion_id": 1, "met": "yes"}')
def test_the_rubric_autosave_answers_any_body_with_json(rubric: dict[str, Any], body: bytes) -> None:
    url = reverse("orange_team:assignment_save", args=[rubric["assignment"].pk])
    with rolled_back():
        response = rubric["client"].post(url, body, content_type="application/json")
        assert response["Content-Type"] == "application/json"
        answer = response.json()
        assert response.status_code == 200 or answer["error"]


@given(assignment_id=st.integers(0, 2**63 - 1))
@example(assignment_id=0)
def test_the_rubric_autosave_answers_a_missing_assignment_with_a_json_404(
    rubric: dict[str, Any], assignment_id: int
) -> None:
    assume(assignment_id != rubric["assignment"].pk)
    body = json.dumps({"criterion_id": rubric["criterion"].pk, "met": True})
    url = reverse("orange_team:assignment_save", args=[assignment_id])
    response = rubric["client"].post(url, body, content_type="application/json")
    assert response.status_code == 404
    assert response.json()["error"]
