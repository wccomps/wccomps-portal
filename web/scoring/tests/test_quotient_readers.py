"""What the Quotient client reads from each of Quotient's answers, and how it signs in: the values, not only their
types (the reader property tests in core/tests/test_properties_readers.py check those, and accept None for any
answer)."""

import json
from collections.abc import Callable

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings
from quotient import client as client_module
from quotient.client import (
    Inject,
    QuotientBox,
    QuotientClient,
    QuotientInfrastructure,
    QuotientService,
    ServiceCheck,
    ServiceExportEntry,
    TeamServiceExport,
    TeamUptime,
)

from core.tests.external_json import Answer, serving


def _read[T](body: object, get: Callable[[QuotientClient], T]) -> T:
    """What get makes of Quotient answering body (after a sign-in), with nothing cached."""
    cache.clear()
    answers = [Answer(200, b"{}"), Answer(200, json.dumps(body).encode())]
    with override_settings(QUOTIENT_USERNAME="admin", QUOTIENT_PASSWORD="secret"), serving(answers):
        return get(QuotientClient(base_url="https://quotient.test"))


INJECT = {"ID": 3, "Title": "Inject 03 - Firewall (1,250 pts)", "Description": "Write the rules"}


@pytest.mark.parametrize("body", [[INJECT], {"injects": [INJECT]}], ids=["list", "object"])
def test_injects_read_each_id_title_description_and_points(body: object) -> None:
    injects = _read(body, lambda c: c.get_injects(force_refresh=True))

    assert injects == [Inject(inject_id=3, title=INJECT["Title"], description="Write the rules", max_points=1250)]


def test_infrastructure_reads_each_box_its_ip_and_services() -> None:
    body = {"boxes": [{"name": "web", "ip": "10.0.0.5", "services": ["http", "ssh"]}, {"name": "db", "ip": "10.0.0.6"}]}

    infrastructure = _read(body, lambda c: c.get_infrastructure(force_refresh=True))

    assert infrastructure == QuotientInfrastructure(
        boxes=[
            QuotientBox(
                name="web",
                ip="10.0.0.5",
                services=[QuotientService("http", "http"), QuotientService("ssh", "ssh")],
            ),
            QuotientBox(name="db", ip="10.0.0.6", services=[]),
        ]
    )


def test_metadata_without_boxes_reads_as_no_boxes() -> None:
    assert _read({}, lambda c: c.get_infrastructure(force_refresh=True)) == QuotientInfrastructure(boxes=[])


def test_a_check_without_a_start_time_debug_or_error_reads_them_as_blank() -> None:
    checks = _read([{"Round": {"ID": 7}, "Result": True}], lambda c: c.get_service_checks(1, "web:http"))

    assert checks == [ServiceCheck(round_number=7, timestamp=None, passed=True, debug="", error="")]


def test_the_service_export_reads_each_teams_points_and_services() -> None:
    body = [
        {
            "team_name": "team09",
            "services": [{"service_name": "web-http", "service_points": 120, "sla_violations": 2, "sla_penalty": 10}],
            "gross_points": 120,
            "total_sla_penalty": 10,
            "total_points": 110,
        }
    ]

    assert _read(body, lambda c: c.get_service_export()) == [
        TeamServiceExport(
            team_name="team09",
            team_number=9,
            services=[ServiceExportEntry("web-http", service_points=120, sla_violations=2, sla_penalty=10)],
            gross_points=120,
            total_sla_penalty=10,
            total_points=110,
        )
    ]


def test_an_export_leaving_out_what_a_team_has_none_of_reads_it_as_zero() -> None:
    body = [
        {"team_name": "team10", "services": [{"service_name": "web-http", "service_points": 5}]},
        {"team_name": "x"},
    ]

    assert _read(body, lambda c: c.get_service_export()) == [
        TeamServiceExport(
            team_name="team10",
            team_number=10,
            services=[ServiceExportEntry("web-http", service_points=5, sla_violations=0, sla_penalty=0)],
            gross_points=0,
            total_sla_penalty=0,
            total_points=0,
        ),
        TeamServiceExport(
            team_name="x", team_number=0, services=[], gross_points=0, total_sla_penalty=0, total_points=0
        ),
    ]


def test_uptimes_read_each_teams_services() -> None:
    body = {
        "series": [
            {"Name": "team02", "Data": [{"Service": "web-http", "Uptime": 0.25}, {"Service": "db-sql", "Uptime": 1}]},
            {"Name": "team03"},
        ]
    }

    assert _read(body, lambda c: c.get_uptimes()) == [
        TeamUptime(team_name="team02", team_number=2, uptimes={"web-http": 0.25, "db-sql": 1.0}),
        TeamUptime(team_name="team03", team_number=3, uptimes={}),
    ]


def test_uptimes_without_a_series_read_as_no_teams() -> None:
    assert _read({}, lambda c: c.get_uptimes()) == []


def _seen_by(monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """The requests the client sends, each answered by answer, with nothing cached."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    real_client = httpx.Client
    monkeypatch.setattr(client_module.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handler)))
    cache.clear()
    return seen


def _timeout(seconds: int) -> dict[str, int]:
    return {"connect": seconds, "read": seconds, "write": seconds, "pool": seconds}


def test_an_expired_session_signs_in_again_and_asks_once_more(monkeypatch, settings) -> None:
    settings.QUOTIENT_USERNAME = "admin"
    settings.QUOTIENT_PASSWORD = "secret"
    reads: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={})
        reads.append(1)
        return httpx.Response(401) if len(reads) == 1 else httpx.Response(200, json=[INJECT])

    seen = _seen_by(monkeypatch, answer)

    injects = QuotientClient(base_url="https://quotient.test/").get_injects()

    assert injects is not None
    assert [i.inject_id for i in injects] == [3]
    assert [str(r.url) for r in seen] == ["https://quotient.test/api/login", "https://quotient.test/api/injects"] * 2
    assert [r.extensions["timeout"] for r in seen[1::2]] == [_timeout(10)] * 2


def test_one_sign_in_serves_every_later_read() -> None:
    cache.clear()
    answers = [Answer(200, b"{}"), Answer(200, b'{"boxes": []}'), Answer(200, b"[]")]
    with override_settings(QUOTIENT_USERNAME="admin", QUOTIENT_PASSWORD="secret"), serving(answers) as requests:
        client = QuotientClient(base_url="https://quotient.test")
        assert client.get_infrastructure(force_refresh=True) == QuotientInfrastructure(boxes=[])
        assert client.get_injects(force_refresh=True) == []

    assert requests == [
        "https://quotient.test/api/login",
        "https://quotient.test/api/metadata",
        "https://quotient.test/api/injects",
    ]


def test_the_sign_in_sends_the_configured_account_with_the_configured_timeout(monkeypatch, settings) -> None:
    settings.QUOTIENT_USERNAME = "admin"
    settings.QUOTIENT_PASSWORD = "secret"
    settings.HTTPX_DEFAULT_TIMEOUT = 7
    seen = _seen_by(monkeypatch, lambda r: httpx.Response(200, json={} if r.url.path == "/api/login" else []))

    assert QuotientClient(base_url="https://quotient.test").get_injects(force_refresh=True) == []
    login, read = seen
    assert str(login.url) == "https://quotient.test/api/login"
    assert json.loads(login.content) == {"username": "admin", "password": "secret"}
    assert login.extensions["timeout"] == _timeout(7)
    assert read.extensions["timeout"] == _timeout(10)


@pytest.mark.parametrize(("username", "password"), [("admin", ""), ("", "secret")])
def test_a_missing_username_or_password_asks_quotient_nothing(username: str, password: str) -> None:
    cache.clear()
    with (
        override_settings(QUOTIENT_USERNAME=username, QUOTIENT_PASSWORD=password),
        serving([Answer(200, b"[]")]) as requests,
    ):
        assert QuotientClient(base_url="https://quotient.test").get_injects(force_refresh=True) is None
    assert requests == []
