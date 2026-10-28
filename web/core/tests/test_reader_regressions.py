"""Regression tests for bugs the reader property tests found (test_properties_readers), each pinned with the
answer that showed it."""

import contextlib
import json

import pytest
from django.core.cache import cache
from django.test import override_settings
from hypothesis import example, given
from hypothesis import strategies as st
from quotient.client import QuotientClient

from core.authentik_manager import AuthentikAPIError, AuthentikManager
from core.tests.external_json import Answer, answer_lists, deep, near, serving
from core.tests.strategies import rolled_back

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
        with contextlib.suppress(AuthentikAPIError):
            assert isinstance(authentik().get_application_by_slug("netbird"), dict | None)
        with contextlib.suppress(AuthentikAPIError):
            assert isinstance(authentik().get_blueteam_binding("app-1"), dict | None)


@example(answers=[answer({**PROVIDERS, "pagination": {"next": 2}})])
@given(answers=answer_lists(PROVIDERS))
def test_a_list_that_never_ends_stops(answers: list[Answer]) -> None:
    """A provider list whose every page said there was a next one was read forever (list_applications), and
    _list_all likewise: the page and the refresh loop hung."""
    with serving(answers), contextlib.suppress(AuthentikAPIError):
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
def test_a_user_of_another_shape_is_an_authentik_error_or_no_user(answers: list[Answer]) -> None:
    """get_user_with_groups returned whatever the first result was: a pk that wasn't a number, or groups that
    weren't objects, failed the readiness checks and their group fix."""
    with serving(answers):
        try:
            user = authentik().get_user_with_groups("team01")
        except AuthentikAPIError:
            return
    assert user is None or (type(user["pk"]) is int and all(isinstance(g, dict) for g in user.get("groups_obj", [])))


@example(slug=None)
@given(slug=st.one_of(st.none(), st.integers(), st.lists(st.text(max_size=3), max_size=2)))
def test_an_application_without_a_text_pk_is_an_authentik_error(slug: object) -> None:
    """get_application_by_slug returned any JSON as the application, and its callers read app["pk"]."""
    with serving([answer({"pk": slug, "slug": "netbird"})]), pytest.raises(AuthentikAPIError):
        authentik().get_application_by_slug("netbird")


QUOTIENT = {"QUOTIENT_USERNAME": "property", "QUOTIENT_PASSWORD": "property"}
LOGIN = answer({})
METADATA = {"boxes": [{"name": "web", "ip": "10.0.0.5", "services": ["http"]}]}
EXPORT = [{"team_name": "team01", "services": [{"service_name": "web", "service_points": 1}], "gross_points": 1}]


def quotient() -> QuotientClient:
    cache.clear()
    return QuotientClient(base_url="https://quotient.test")


@example(body=[{"name": 5, "ip": "10.0.0.5"}])
@example(body={"boxes": [{"name": "web", "ip": None, "services": [7]}]})
@example(body={"boxes": "web"})
@given(body=near(METADATA))
def test_infrastructure_of_another_shape_is_none(body: object) -> None:
    """get_infrastructure caught only KeyError and ValueError: metadata that was a list (AttributeError), or boxes
    of another type (TypeError), escaped; a box name or IP that wasn't text came back, and the ticket form and the
    bot's autocomplete called .lower() on it."""
    with override_settings(**QUOTIENT), serving([LOGIN, answer(body)]):
        infrastructure = quotient().get_infrastructure(force_refresh=True)
    assert infrastructure is None or all(
        isinstance(b.name, str) and isinstance(b.ip, str) for b in infrastructure.boxes
    )


@example(body=[{**EXPORT[0], "gross_points": "1"}])
@example(body=[{**EXPORT[0], "gross_points": 1.5}])
@example(body={"team_name": "team01"})
@given(body=near(EXPORT))
def test_scores_of_another_shape_are_none(body: object) -> None:
    """get_service_export passed any JSON value along as points, and the sync wrote it to a decimal column."""
    with override_settings(**QUOTIENT), serving([LOGIN, answer(body)]):
        exports = quotient().get_service_export(force_refresh=True)
    assert exports is None or all(type(t.gross_points) is int for t in exports)


@example(uptime=float("nan"))
@example(uptime=7)
@given(uptime=st.one_of(st.floats(), st.integers(), st.text(max_size=3)))
def test_an_uptime_that_is_no_fraction_is_refused(uptime: object) -> None:
    """get_uptimes passed NaN, numbers past 1 and text along as uptimes."""
    body = {"series": [{"Name": "team01", "Data": [{"Service": "web", "Uptime": uptime}]}]}
    with override_settings(**QUOTIENT), serving([LOGIN, Answer(200, json.dumps(body).encode())]):
        uptimes = quotient().get_uptimes(force_refresh=True)
    assert uptimes is None or all(0 <= u <= 1 for t in uptimes for u in t.uptimes.values())


@example(answers=[LOGIN, Answer(200, deep(100_000))])
@given(answers=st.just([LOGIN, Answer(200, deep(100_000))]))
def test_json_too_deep_to_read_is_no_answer(answers: list[Answer]) -> None:
    """JSON nested past Python's recursion limit raised RecursionError out of every Quotient reader."""
    with override_settings(**QUOTIENT), serving(answers):
        assert quotient().get_injects(force_refresh=True) is None


@pytest.mark.django_db
@example(points=10**30, name="web")
@example(points=1, name="s" * 101)
@given(points=st.integers(), name=st.text(min_size=1, max_size=150).filter(lambda s: "\x00" not in s))
def test_scores_that_dont_fit_refuse_the_sync(points: int, name: str) -> None:
    """sync_service_scores wrote Quotient's points and service names to their columns unchecked: points past
    decimal(10, 2), or a name past 100 characters, failed the write halfway (DataError) instead of the sync saying
    why."""
    from scoring.quotient_sync import sync_service_scores

    from team.models import Team

    body = [{"team_name": "team01", "services": [{"service_name": name, "service_points": points}], "gross_points": 1}]
    with rolled_back():
        Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01"})
        with (
            override_settings(QUOTIENT_API_URL="https://quotient.test", **QUOTIENT),
            serving([LOGIN, answer(body), answer({"series": []})]),
        ):
            cache.clear()
            with contextlib.suppress(ValueError):
                sync_service_scores()
        assert Team.objects.filter(team_number=1).exists()  # the transaction still works


@pytest.mark.django_db
@example(export=Answer(500, b""), uptimes=answer({"series": []}))
@example(
    export=answer(
        [{"team_name": "team01", "services": [{"service_name": "web", "service_points": 10}], "gross_points": 10}]
    ),
    uptimes=Answer(500, b""),
)
@given(
    export=st.sampled_from(
        (
            Answer(500, b""),
            answer(
                [
                    {
                        "team_name": "team01",
                        "services": [{"service_name": "web", "service_points": 10}],
                        "gross_points": 10,
                    }
                ]
            ),
        )
    ),
    uptimes=st.one_of(
        st.builds(Answer, st.sampled_from((400, 404, 429, 500, 502, 503)), st.binary(max_size=40)),
        st.text(max_size=20).map(answer),
    ),
)
def test_unreadable_export_or_uptimes_refuses_the_sync(export: Answer, uptimes: Answer) -> None:
    """When the score export failed, sync_service_scores returned 0 teams (a green "Synced 0 teams" banner); when
    uptimes failed, it overwrote every service's uptime with 0."""
    from decimal import Decimal

    from scoring.models import ServiceDetail
    from scoring.quotient_sync import sync_service_scores

    from team.models import Team

    with rolled_back():
        team, _ = Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01"})
        ServiceDetail.objects.create(team=team, service_name="web", points=Decimal("5.00"), uptime=Decimal("0.9500"))
        with override_settings(QUOTIENT_API_URL="https://quotient.test", **QUOTIENT), serving([LOGIN, export, uptimes]):
            cache.clear()
            with pytest.raises(ValueError):
                sync_service_scores()
        assert ServiceDetail.objects.get(team=team, service_name="web").uptime == Decimal("0.9500")


OAUTH_CONFIG = {
    "client_id": "property-client",
    "client_secret": "property-secret",
    "authorization_endpoint": "https://auth.test/authorize/",
    "token_endpoint": "https://auth.test/token/",
    "userinfo_endpoint": "https://auth.test/userinfo/",
    "end_session_endpoint": "https://auth.test/end-session/",
}
TOKENS = answer({"access_token": "access"})
USERINFO = {"sub": "sub-1", "preferred_username": "player", "email": "player@example.edu", "groups": ["G"]}


from core.tests.views_harness import views_profile


@pytest.mark.django_db
@views_profile()
@example(answers=[Answer(200, b"<html>502 Bad Gateway</html>")])
@example(answers=[answer(["access"])])
@example(answers=[TOKENS, answer({**USERINFO, "groups": "WCComps_BlueTeam01"})])
@example(answers=[TOKENS, answer({**USERINFO, "sub": 7})])
@example(answers=[TOKENS, answer({**USERINFO, "preferred_username": "p" * 200})])
@given(answers=answer_lists({"access_token": "access"}, USERINFO))
def test_a_sign_in_takes_only_usable_answers(answers: list[Answer]) -> None:
    """The OAuth callback caught only HTTP errors: a token or userinfo answer that wasn't JSON, or wasn't an object,
    raised (500), and userinfo claims of the wrong type were stored (groups as a string, read as one group per
    letter; a sub that wasn't text; a username past its column failed the insert)."""
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlparse

    from django.test import Client

    from core.models import UserGroups

    with rolled_back():
        client = Client()
        with patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG):
            state = parse_qs(urlparse(client.get("/auth/login/")["Location"]).query)["state"][0]
            with serving(answers):
                response = client.get("/auth/callback/", {"code": "code", "state": state})
        assert response.status_code < 500
        for row in UserGroups.objects.all():
            assert isinstance(row.groups, list) and all(isinstance(g, str) for g in row.groups)
