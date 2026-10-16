"""Requests the view property tests (views_harness) found breaking a view, each pinned with @example of a failing
input, so each fix is held by a test of its own."""

import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from hypothesis import assume, example, given
from hypothesis import strategies as st
from orange_team.models import OrangeAssignment, OrangeAssignmentResult, OrangeCheck, OrangeCheckCriterion
from registration.models import TeamRegistration

from core.models import BotState
from core.tests.roles import role_user
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


# ---------------------------------------------------------------------------------------------------------------
# Only a pending registration is approved or rejected
# ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["registration_approve", "registration_reject"])
@given(status=st.sampled_from(["approved", "paid", "credentials_sent", "rejected"]))
@example(status="paid")
def test_only_a_pending_registration_is_approved_or_rejected(admin_client: Client, action: str, status: str) -> None:
    """Approving a paid registration (a second click, a stale page) put it back to approved."""
    with rolled_back():
        registration = TeamRegistration.objects.create(school_name="School", status=status)
        before = TeamRegistration.objects.filter(pk=registration.pk).values().get()
        admin_client.post(reverse(action, args=[registration.pk]), {"reason": "Late"})
        assert TeamRegistration.objects.filter(pk=registration.pk).values().get() == before


# ---------------------------------------------------------------------------------------------------------------
# A lead checking in a missing volunteer gets a 404
# ---------------------------------------------------------------------------------------------------------------


@given(user_id=st.integers(0, 2**63 - 1))
@example(user_id=0)
@example(user_id=2**31)
def test_checking_in_a_missing_volunteer_is_a_404(admin_client: Client, user_id: int) -> None:
    assume(not User.objects.filter(pk=user_id).exists())
    response = admin_client.post(reverse("orange_team:admin_toggle_checkin", args=[user_id]))
    assert response.status_code == 404


# ---------------------------------------------------------------------------------------------------------------
# The Django admin
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def superuser_client(admin_user: User) -> Client:
    User.objects.filter(pk=admin_user.pk).update(is_staff=True, is_superuser=True)
    return signed_in(admin_user)


@pytest.mark.parametrize("page", ["change", "delete"])
@given(method=st.sampled_from(["get", "post"]))
@example(method="get")
def test_the_admin_refuses_an_empty_to_field(superuser_client: Client, page: str, method: str) -> None:
    """Django's change and delete pages look an empty _to_field up as a field named "" and fail with a 500."""
    url = reverse(f"admin:core_botstate_{page}", args=[1])
    with rolled_back():
        BotState.objects.create(pk=1, key="k", value="v")
        response = getattr(superuser_client, method)(f"{url}?_to_field=", {"_to_field": ""})
    assert response.status_code == 400


def test_the_admin_reads_to_field_as_django_does(superuser_client: Client) -> None:
    """Django reads the POSTed _to_field before the query's: a POSTed one stands even with an empty query one."""
    url = reverse("admin:core_botstate_change", args=[1])
    with rolled_back():
        BotState.objects.create(pk=1, key="k", value="v")
        response = superuser_client.post(f"{url}?_to_field=", {"_to_field": "id", "key": "k", "value": "v"})
    assert response.status_code != 400


@pytest.mark.parametrize("page", ["change", "delete"])
def test_django_still_fails_on_an_empty_to_field(admin_user: User, page: str) -> None:
    """The canary for AuthentikAdminSite.admin_view's empty _to_field refusal: Django's own page, without the
    portal's wrapper, still fails. When this fails, Django has fixed it: drop the override in core/admin.py."""
    from django.contrib import admin
    from django.core.exceptions import FieldDoesNotExist
    from django.test import RequestFactory

    BotState.objects.create(pk=1, key="k", value="v")
    User.objects.filter(pk=admin_user.pk).update(is_staff=True, is_superuser=True)
    request = RequestFactory().get("/", {"_to_field": ""})
    request.user = User.objects.get(pk=admin_user.pk)
    model_admin = admin.site._registry[BotState]
    view = model_admin.change_view if page == "change" else model_admin.delete_view
    with pytest.raises(FieldDoesNotExist):
        view(request, "1")


@given(param=st.text(min_size=1, max_size=20).filter(lambda p: "\x00" not in p))
@example(param="next")
def test_the_admin_list_shows_its_error_page_for_an_unknown_filter(superuser_client: Client, param: str) -> None:
    """Django redirects a list asked for a filter it doesn't have to ?e=1, then renders its invalid-setup page
    without a request, which the portal's admin header broke: it read user.username with no user in the page."""
    assume(param not in ("q", "o", "p", "e", "all", "_popup", "_to_field") and not param.startswith("_"))
    with rolled_back():
        response = superuser_client.get(reverse("admin:core_botstate_changelist"), {param: "x", "e": "1"})
    assert response.status_code in (200, 302)


# ---------------------------------------------------------------------------------------------------------------
# A NUL character in a cookie is refused before the session is read
# ---------------------------------------------------------------------------------------------------------------


# A header arrives as bytes Django decodes as Latin-1, so a cookie holds only characters below 256
latin1_with_nul = with_nul.map(lambda t: t.encode("latin-1", "replace").decode("latin-1"))


@given(name=st.sampled_from(["sessionid", "csrftoken", "tz", "other"]), text=latin1_with_nul)
@example(name="sessionid", text="abcdefgh\x00xyz")
def test_a_nul_character_in_a_cookie_is_refused(name: str, text: str) -> None:
    """The session key reached PostgreSQL, so anyone got a 500 (and paged the error webhook) with one cookie."""
    client = Client()
    client.cookies[name] = text
    with rolled_back():
        response = client.get(reverse("ticket_list"))
    assert response.status_code == 400


def test_an_anonymous_post_body_is_not_read_before_the_sign_in_gate() -> None:
    """Only signed-in requests (and the public pages) get their form fields parsed for a NUL."""
    response = Client().post(reverse("ticket_list"), {"search": "\x00"})
    assert response.status_code == 302
    assert response["Location"].startswith("/auth/login/")


# ---------------------------------------------------------------------------------------------------------------
# The ticket form puts posted values back into its script as JavaScript strings
# ---------------------------------------------------------------------------------------------------------------

JS_STRING = re.compile(r"selectedHostname: '((?:[^'\\\n\r  ]|\\.)*)',\n")


@given(hostname=request_text(60).filter(lambda h: "\x00" not in h))
@example(hostname="a\\")
@example(hostname="a\nb")
def test_the_ticket_form_writes_posted_text_into_its_script_as_a_string(
    blue_team_user: User, tickets: dict[str, Any], hostname: str
) -> None:
    """HTML escaping left a backslash or a line break that ended or broke the JavaScript string (self only)."""
    client = signed_in(blue_team_user)
    with rolled_back():
        response = client.post(reverse("create_ticket"), {"hostname": hostname})  # no title: the form comes back
    assert JS_STRING.search(response.content.decode()), "the posted hostname broke the script's string"


@pytest.mark.parametrize(("role", "let_in"), [("admin", True), ("ticketing_admin", False), ("gold_team", False)])
def test_the_django_admin_lets_in_only_the_admin_permission(role: str, let_in: bool) -> None:
    """One rule for /admin/, the sign-in gate's: the admin site let Ticketing Admins in where the gate didn't."""
    from django.contrib import admin
    from django.test import RequestFactory

    request = RequestFactory().get("/admin/")
    request.user = role_user(role)
    assert admin.site.has_permission(request) is let_in
