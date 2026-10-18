"""Regression tests for bugs the reader property tests found (test_properties_readers), each pinned with the
answer that showed it."""

import json

from hypothesis import example, given
from hypothesis import strategies as st

from core.authentik_manager import AuthentikAPIError, AuthentikManager
from core.tests.external_json import Answer, answer_lists, deep, serving

USER = {"pk": 7, "username": "team01", "uid": "uid-7", "is_active": True, "groups": ["g1"], "groups_obj": []}
USERS = {"results": [USER], "pagination": {"next": 0}}
PROVIDERS = {"results": [{"assigned_application_slug": "netbird"}], "pagination": {"next": 0}}


def answer(body: object, status: int = 200) -> Answer:
    return Answer(status, json.dumps(body).encode())


def authentik() -> AuthentikManager:
    return AuthentikManager(base_url="https://auth.test", api_token="token")


@example(answers=[answer(["not", "an", "object"], status=400)])
@example(answers=[answer(None, status=502)])
@given(answers=answer_lists({"detail": "Not allowed"}))
def test_an_error_answer_of_any_shape_reads_as_an_authentik_error(answers: list[Answer]) -> None:
    """An error answer whose JSON wasn't an object raised AttributeError while reading its detail, inside the
    readers' except clauses, so it escaped them (the competition page's app check answered 500)."""
    with serving(answers):
        assert isinstance(authentik().get_application_by_slug("netbird"), dict | None)
        binding, error = authentik().get_blueteam_binding("app-1")
        assert binding is None or isinstance(binding, dict)


@example(answers=[answer({**PROVIDERS, "pagination": {"next": 2}})])
@given(answers=answer_lists(PROVIDERS))
def test_a_list_that_never_ends_stops(answers: list[Answer]) -> None:
    """A provider list whose every page said there was a next one was read forever (list_applications), and
    _list_all likewise: the page and the refresh loop hung."""
    with serving(answers):
        assert isinstance(authentik().list_applications(), list)


@example(answers=[Answer(200, deep(100_000))])
@example(answers=[answer({"results": [{"uid": 7}], "pagination": {"next": 0}})])
@given(answers=answer_lists(USERS))
def test_a_user_list_of_another_shape_is_an_authentik_error(answers: list[Answer]) -> None:
    """list_all_users raised KeyError, TypeError or RecursionError (JSON nested past the recursion limit) for an
    answer of another shape, where its callers expect AuthentikAPIError; a uid that wasn't text reached the group
    refresh."""
    with serving(answers):
        try:
            users = authentik().list_all_users()
        except AuthentikAPIError:
            return
    assert all(isinstance(u["uid"], str) and type(u["is_active"]) is bool for u in users)


@example(answers=[answer({"results": [{**USER, "pk": "7"}]})])
@example(answers=[answer({"results": [{**USER, "groups_obj": ["WCComps_BlueTeam01"]}]})])
@example(answers=[answer({"results": "none"})])
@given(answers=answer_lists(USERS))
def test_a_user_of_another_shape_is_no_user(answers: list[Answer]) -> None:
    """get_user_with_groups returned whatever the first result was: a pk that wasn't a number, or groups that
    weren't objects, failed the readiness checks and their group fix."""
    with serving(answers):
        user = authentik().get_user_with_groups("team01")
    assert user is None or (type(user["pk"]) is int and all(isinstance(g, dict) for g in user.get("groups_obj", [])))


@example(slug=None)
@given(slug=st.one_of(st.none(), st.integers(), st.lists(st.text(max_size=3), max_size=2)))
def test_an_application_without_a_text_pk_is_not_found(slug: object) -> None:
    """get_application_by_slug returned any JSON as the application, and its callers read app["pk"]."""
    with serving([answer({"pk": slug, "slug": "netbird"})]):
        app = authentik().get_application_by_slug("netbird")
    assert app is None or isinstance(app["pk"], str)
