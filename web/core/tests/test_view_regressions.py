"""Requests the view property tests (views_harness) found breaking a view, each pinned with @example of a failing
input, so each fix is held by a test of its own."""

import json
import re
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.db.models import Model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from hypothesis import assume, example, given
from hypothesis import strategies as st
from orange_team.models import OrangeAssignment, OrangeAssignmentResult, OrangeCheck, OrangeCheckCriterion
from registration.models import TeamRegistration
from scoring.models import IncidentReport

from core.middleware import NUL_MESSAGE
from core.models import BotState, UserGroups
from core.tests.roles import role_user
from core.tests.strategies import request_text, rolled_back
from team.models import Team
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
def tickets(db: Any, default_category: Any, blue_team) -> dict[str, Any]:
    """Team 1's ticket (the blue_team role's, by its Authentik group) and team 2's."""
    own = blue_team
    other = Team.objects.create(team_number=2, team_name="Team 02")
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
        ("ticketing_support", "get", lambda t: reverse("create_ticket"), 403),
        ("admin", "post", lambda t: reverse("ticket_cancel", args=[t["own"].ticket_number]), 403),
        ("blue_team", "post", lambda t: reverse("ticket_cancel", args=[t["other"].ticket_number]), 404),
        ("admin", "get", lambda t: reverse("school_info_edit", args=[99]), 404),
        ("admin", "get", lambda t: reverse("admin_team_detail", args=[99]), 404),
    ],
)
def test_a_refusal_page_carries_its_status(
    tickets: dict[str, Any],
    role: str,
    method: str,
    url: Callable[[dict[str, Any]], str],
    status: int,
) -> None:
    """These pages said "Access denied" or "not found" with a 200, so nothing could tell them from a success."""
    client = signed_in(role_user(role, username=f"refused-{role}"))
    response = getattr(client, method)(url(tickets))
    assert response.status_code == status
    tickets["own"].refresh_from_db()
    assert tickets["own"].status == "open"


# ---------------------------------------------------------------------------------------------------------------
# The rubric autosave answers any body with JSON
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture
def rubric(orange_team_user: User, blue_team) -> dict[str, Any]:
    check = OrangeCheck.objects.create(title="Phones", description="", max_points=5)
    criterion = OrangeCheckCriterion.objects.create(orange_check=check, label="Answered", points=5, sort_order=1)
    team = blue_team
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
def admin_site_client(admin_user: User) -> Client:
    return signed_in(admin_user)


@pytest.mark.parametrize("page", ["change", "delete"])
@given(method=st.sampled_from(["get", "post"]))
@example(method="get")
def test_the_admin_refuses_an_empty_to_field(admin_site_client: Client, page: str, method: str) -> None:
    """Django's change and delete pages look an empty _to_field up as a field named "" and fail with a 500."""
    url = reverse(f"admin:core_botstate_{page}", args=[1])
    with rolled_back():
        BotState.objects.create(pk=1, key="k", value="v")
        response = getattr(admin_site_client, method)(f"{url}?_to_field=", {"_to_field": ""})
    assert response.status_code == 400


def test_the_admin_reads_to_field_as_django_does(admin_site_client: Client) -> None:
    """Django reads the POSTed _to_field before the query's: a POSTed one stands even with an empty query one."""
    url = reverse("admin:core_botstate_change", args=[1])
    with rolled_back():
        BotState.objects.create(pk=1, key="k", value="v")
        response = admin_site_client.post(f"{url}?_to_field=", {"_to_field": "id", "key": "k", "value": "v"})
    assert response.status_code != 400


@pytest.mark.parametrize("page", ["change", "delete"])
def test_django_still_fails_on_an_empty_to_field(admin_user: User, page: str) -> None:
    """The canary for AuthentikAdminSite.admin_view's empty _to_field refusal: Django's own page, without the
    portal's wrapper, still fails. When this fails, Django has fixed it: drop the override in core/admin.py."""
    from django.contrib import admin
    from django.core.exceptions import FieldDoesNotExist
    from django.test import RequestFactory

    BotState.objects.create(pk=1, key="k", value="v")
    request = RequestFactory().get("/", {"_to_field": ""})
    request.user = User.objects.get(pk=admin_user.pk)
    model_admin = admin.site._registry[BotState]
    view = model_admin.change_view if page == "change" else model_admin.delete_view
    with pytest.raises(FieldDoesNotExist):
        view(request, "1")


@given(param=st.text(min_size=1, max_size=20).filter(lambda p: "\x00" not in p))
@example(param="next")
def test_the_admin_list_shows_its_error_page_for_an_unknown_filter(admin_site_client: Client, param: str) -> None:
    """Django redirects a list asked for a filter it doesn't have to ?e=1, then renders its invalid-setup page
    without a request, which the portal's admin header broke: it read user.username with no user in the page."""
    assume(param not in ("q", "o", "p", "e", "all", "_popup", "_to_field") and not param.startswith("_"))
    with rolled_back():
        response = admin_site_client.get(reverse("admin:core_botstate_changelist"), {param: "x", "e": "1"})
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
    client = Client(HTTP_COOKIE=f"{name}={text}")  # raw, as anyone can send it (the test client's jar refuses it)
    with rolled_back():
        response = client.get(reverse("ticket_list"))
    assert response.status_code == 400


def test_a_quoted_octal_nul_in_a_cookie_is_refused() -> None:
    """Django's cookie parser turns a quoted octal escape into the character itself: the first form reported."""
    with rolled_back():
        response = Client(HTTP_COOKIE='sessionid="abcdefgh\\000xyz"').get(reverse("ticket_list"))
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/tickets/", "/livez/", "/health/", "/static/nope", "/auth/link"])
def test_an_anonymous_post_body_is_not_parsed_for_a_nul(path: str) -> None:
    """Only a signed-in request's form fields are parsed for a NUL; the sign-in gate lets anonymous POSTs through
    to the health checks, /static/ and the sign-in and link paths (/auth/login/, /auth/callback/, /auth/logout/,
    /auth/link), whose bodies nothing reads."""
    response = Client().post(path, {"search": "\x00"})
    assert NUL_MESSAGE.encode() not in response.content


# ---------------------------------------------------------------------------------------------------------------
# The ticket form puts posted values back into its script as JavaScript strings
# ---------------------------------------------------------------------------------------------------------------

JS_STRING = re.compile(r"selectedHostname: '((?:[^'\\\n\r\u2028\u2029]|\\.)*)',\n")


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
    """One rule for the Django admin, the admin permission: the admin site let Ticketing Admins in where the sign-in
    gate (which then also guarded /admin/) didn't."""
    from django.contrib import admin
    from django.test import RequestFactory

    request = RequestFactory().get("/admin/")
    request.user = role_user(role)
    assert admin.site.has_permission(request) is let_in


JS_STRING_LIST = re.compile(r"apps: \[((?:'(?:[^'\\\n\r\u2028\u2029]|\\.)*'(?:, )?)*)\],\n")


@given(names=st.lists(request_text(30).filter(lambda n: "\x00" not in n), min_size=1, max_size=3))
@example(names=["a'b", "c\\d", "e\nf"])
def test_the_setup_page_writes_app_names_into_its_script_as_strings(admin_client: Client, names: list[str]) -> None:
    """App names come from the admin's JSON field too: a backslash or line break broke the script (and a quote showed as
    &#x27;)."""
    from core.models import CompetitionConfig

    with rolled_back():
        config = CompetitionConfig.get_config()
        config.controlled_applications = names
        config.save()
        response = admin_client.get(reverse("admin_competition_setup"))
    assert JS_STRING_LIST.search(response.content.decode()), "an app name broke the script's list of strings"


def test_an_htmx_request_with_a_nul_gets_a_plain_refusal(admin_client: Client) -> None:
    """htmx doesn't read JSON (it doesn't swap an error answer; base_site.html alerts), so a JSON error is for a
    fetch() only."""
    headers = {"HX-Request": "true", "Sec-Fetch-Mode": "cors"}
    response = admin_client.get(reverse("ops_review_tickets"), {"search": "\x00"}, headers=headers)
    assert response.status_code == 400
    assert response["Content-Type"].startswith("text/plain")


def test_the_admin_site_alone_decides_who_gets_into_the_django_admin(gold_team_user: User) -> None:
    """The admin site alone refuses the Django admin's pages; patching its has_permission lets the user in."""
    from django.contrib import admin

    client = signed_in(gold_team_user)
    assert client.get(reverse("admin:index")).status_code == 403
    with patch.object(type(admin.site), "has_permission", lambda self, request: True):
        assert client.get(reverse("admin:index")).status_code == 200


def test_an_incident_report_refuses_a_role_it_doesnt_let_in_with_a_403(red_team_user: User) -> None:
    """A refusal by role answered with an error redirect to the leaderboard, which the role check took for a page
    that let the role in."""
    client = signed_in(red_team_user)
    team = Team.objects.create(team_number=2, team_name="Team 02")
    incident = IncidentReport.objects.create(
        team=team, source_ip="10.0.0.9", attack_detected_at=timezone.now(), attack_description="x"
    )
    assert client.get(reverse("scoring:submit_incident_report")).status_code == 403
    assert client.get(reverse("scoring:view_incident_report", args=[incident.pk])).status_code == 403


@pytest.mark.parametrize("page", ["index", "login"])
def test_the_admin_site_refuses_a_signed_in_non_admin_itself(gold_team_user: User, page: str) -> None:
    """The admin site answers a signed-in visitor it refuses with a 403, not its login form (signing in is
    Authentik's), so the sign-in gate needs no admin rule of its own."""
    from django.contrib import admin
    from django.test import RequestFactory

    request = RequestFactory().get(reverse(f"admin:{page}"))
    request.user = gold_team_user
    view = admin.site.login if page == "login" else admin.site.admin_view(admin.site.index)
    assert view(request).status_code == 403


def _registered_models() -> list[type[Model]]:
    from django.contrib import admin

    return sorted(admin.site._registry, key=lambda model: model._meta.label)


def test_an_admin_who_isnt_a_superuser_can_use_every_admin_model(admin_user: User) -> None:
    """Production's admins hold the admin permission (an Authentik group) and no Django flags: each model page checks
    Django's own permissions, which refused them everything."""
    client = signed_in(admin_user)
    user = User.objects.get(pk=admin_user.pk)
    assert not user.is_superuser and not user.is_staff
    for model in _registered_models():
        meta = model._meta
        for action in ("view", "add", "change", "delete"):
            assert user.has_perm(f"{meta.app_label}.{action}_{meta.model_name}"), (meta.label, action)
        changelist = reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")
        assert client.get(changelist).status_code == 200, meta.label


def test_a_superuser_without_the_admin_permission_cant_get_into_the_admin(gold_team_user: User) -> None:
    """Django's own flags grant a superuser every model permission (has_perm), so the admin site's rule is the only
    thing keeping one without the admin permission out."""
    User.objects.filter(pk=gold_team_user.pk).update(is_staff=True, is_superuser=True)
    user = User.objects.get(pk=gold_team_user.pk)
    assert user.is_superuser and user.is_staff
    client = signed_in(user)
    assert client.get(reverse("admin:index")).status_code == 403
    assert client.get(reverse("admin:core_botstate_changelist")).status_code == 403


def _holds_any_model_permission(user: User | AnonymousUser) -> bool:
    return any(
        user.has_perm(f"{m._meta.app_label}.{action}_{m._meta.model_name}") or user.has_module_perms(m._meta.app_label)
        for m in _registered_models()
        for action in ("view", "add", "change", "delete")
    )


def test_the_admin_permission_grants_nothing_to_a_user_without_it(gold_team_user: User) -> None:
    assert not _holds_any_model_permission(User.objects.get(pk=gold_team_user.pk))


def test_an_anonymous_visitor_holds_no_model_permission() -> None:
    assert not _holds_any_model_permission(AnonymousUser())


def test_an_inactive_admin_holds_no_model_permission(admin_user: User) -> None:
    User.objects.filter(pk=admin_user.pk).update(is_active=False)
    assert not _holds_any_model_permission(User.objects.get(pk=admin_user.pk))


def test_an_admin_whose_admin_group_was_removed_holds_no_model_permission(admin_user: User) -> None:
    UserGroups.objects.filter(user=admin_user).update(groups=[])
    assert not _holds_any_model_permission(User.objects.get(pk=admin_user.pk))


def test_no_backend_grants_a_superuser_without_the_admin_permission_anything(gold_team_user: User) -> None:
    """Django permissions come only from the admin permission: ModelBackend granted a superuser every permission
    (production still has superuser flags). User.has_perm's own superuser shortcut stays; the admin site's gate keeps
    such a user out."""
    from asgiref.sync import async_to_sync
    from django.contrib.auth import get_backends

    User.objects.filter(pk=gold_team_user.pk).update(is_staff=True, is_superuser=True)
    user = User.objects.get(pk=gold_team_user.pk)
    for backend in get_backends():
        assert not backend.has_perm(user, "core.view_botstate"), backend
        assert not backend.has_module_perms(user, "core"), backend
        assert not async_to_sync(backend.ahas_perm)(user, "core.view_botstate"), backend
        assert not async_to_sync(backend.ahas_module_perms)(user, "core"), backend


def test_no_one_signs_in_with_a_password(admin_user: User) -> None:
    """Signing in is Authentik's: ModelBackend also took a username and password (the Django admin's login form)."""
    from django.contrib.auth import authenticate

    admin_user.set_password("a-known-password")
    admin_user.save()
    assert authenticate(username=admin_user.username, password="a-known-password") is None
    response = Client().post(reverse("admin:login"), {"username": admin_user.username, "password": "a-known-password"})
    assert "_auth_user_id" not in response.wsgi_request.session


def test_the_admin_sign_in_sends_a_visitor_to_the_portal_sign_in() -> None:
    """The Django admin's login form took a password no one has: signing in is Authentik's, with the way back kept."""
    from django.conf import settings

    response = Client().get(reverse("admin:login"), {"next": "/admin/core/botstate/"})
    assert response.status_code == 302
    assert response.url == f"{settings.LOGIN_URL}?next=/admin/core/botstate/"
    assert Client().post(reverse("admin:login"), {"username": "a", "password": "b"}).status_code == 302
    assert Client().get(reverse("admin:index")).status_code == 302


def test_a_permission_row_grants_nothing(gold_team_user: User) -> None:
    """A Django Permission row (given in the Django admin) granted that permission outside the admin permission."""
    from django.contrib.auth.models import Permission

    gold_team_user.user_permissions.add(Permission.objects.get(codename="view_botstate"))
    assert not User.objects.get(pk=gold_team_user.pk).has_perm("core.view_botstate")


@pytest.mark.parametrize("state", ["admin", "inactive admin", "admin without groups", "gold_team", "superuser"])
def test_the_admin_site_and_the_permission_backend_agree(state: str) -> None:
    from django.contrib import admin
    from django.contrib.auth import get_backends
    from django.test import RequestFactory

    user = role_user("gold_team" if state in ("gold_team", "superuser") else "admin")
    if state == "inactive admin":
        User.objects.filter(pk=user.pk).update(is_active=False)
    if state == "admin without groups":
        UserGroups.objects.filter(user=user).update(groups=[])
    if state == "superuser":
        User.objects.filter(pk=user.pk).update(is_superuser=True, is_staff=True)
    request = RequestFactory().get("/admin/")
    request.user = User.objects.get(pk=user.pk)
    let_in = admin.site.has_permission(request)
    assert let_in is (state == "admin")
    assert any(backend.has_module_perms(request.user, "core") for backend in get_backends()) is let_in


def test_the_async_permission_checks_follow_the_admin_permission(admin_user: User, gold_team_user: User) -> None:
    """Django's async checks (ahas_perm, ahas_module_perms) go to each backend's async methods, which answered from
    Permission rows only."""
    from asgiref.sync import async_to_sync

    admin, other = User.objects.get(pk=admin_user.pk), User.objects.get(pk=gold_team_user.pk)
    assert async_to_sync(admin.ahas_perm)("core.view_botstate")
    assert async_to_sync(admin.ahas_module_perms)("core")
    assert not async_to_sync(other.ahas_perm)("core.view_botstate")
    assert not async_to_sync(other.ahas_module_perms)("core")
