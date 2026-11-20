"""The Quotient client answers from its cache, an empty answer included, until the answer's lifetime ends or a
refresh is asked for; the score sync's reads always fetch."""

import time
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest
from django.core.cache import cache
from quotient.client import QuotientClient, QuotientInfrastructure

Getter = tuple[str, object, Callable[[QuotientClient], object]]

CACHED: list[Getter] = [
    ("/api/metadata", {"boxes": []}, lambda c: c.get_infrastructure()),
    ("/api/injects", [], lambda c: c.get_injects()),
    ("/api/services/1/web", [], lambda c: c.get_service_checks(1, "web")),
]
UNCACHED: list[Getter] = [
    ("/api/engine/export/scores", [], lambda c: c.get_service_export()),
    ("/api/graphs/uptimes", {"series": []}, lambda c: c.get_uptimes()),
]


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _recording(monkeypatch: pytest.MonkeyPatch, body: object) -> list[str]:
    """The paths the client asks Quotient for, each answered with body."""
    asked: list[str] = []

    def request(self: QuotientClient, method: str, path: str, **kwargs: object) -> httpx.Response:
        asked.append(path)
        return httpx.Response(200, json=body, request=httpx.Request(method.upper(), f"https://quotient.test{path}"))

    monkeypatch.setattr(QuotientClient, "_request", request)
    return asked


def _asked_twice(monkeypatch: pytest.MonkeyPatch, body: object, get: Callable[[QuotientClient], object]) -> list[str]:
    asked = _recording(monkeypatch, body)
    client = QuotientClient(base_url="https://quotient.test")
    first = get(client)
    assert first == get(client)
    assert first == QuotientInfrastructure(boxes=[]) or first == []
    return asked


@pytest.mark.parametrize(("endpoint", "body", "get"), CACHED, ids=[g[0] for g in CACHED])
def test_an_empty_answer_is_cached_like_any_other(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, body: object, get: Callable[[QuotientClient], object]
) -> None:
    assert _asked_twice(monkeypatch, body, get) == [endpoint]


@pytest.mark.parametrize(("endpoint", "body", "get"), UNCACHED, ids=[g[0] for g in UNCACHED])
def test_the_score_syncs_reads_fetch_every_time_and_cache_nothing(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, body: object, get: Callable[[QuotientClient], object]
) -> None:
    assert _asked_twice(monkeypatch, body, get) == [endpoint, endpoint]
    assert cache.get("quotient_service_export") is None
    assert cache.get("quotient_uptimes") is None


Refreshable = tuple[str, object, Callable[[QuotientClient, bool], object], int]

# Each cached read, with how long its answer lasts: the infrastructure for the client's cache_ttl (5 minutes by
# default), the injects a minute, a service's checks 30 seconds
LIFETIMES: list[Refreshable] = [
    ("/api/metadata", {"boxes": []}, lambda c, refresh: c.get_infrastructure(force_refresh=refresh), 300),
    ("/api/injects", [], lambda c, refresh: c.get_injects(force_refresh=refresh), 60),
    ("/api/services/1/web", [], lambda c, refresh: c.get_service_checks(1, "web", force_refresh=refresh), 30),
]


@pytest.mark.parametrize(("endpoint", "body", "get"), [g[:3] for g in LIFETIMES], ids=[g[0] for g in LIFETIMES])
def test_a_forced_refresh_asks_again(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, body: object, get: Callable[[QuotientClient, bool], object]
) -> None:
    asked = _recording(monkeypatch, body)
    client = QuotientClient(base_url="https://quotient.test")
    get(client, False)
    get(client, True)
    assert asked == [endpoint, endpoint]


@pytest.mark.parametrize(("endpoint", "body", "get", "seconds"), LIFETIMES, ids=[g[0] for g in LIFETIMES])
def test_a_cached_answer_lasts_its_lifetime(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    body: object,
    get: Callable[[QuotientClient, bool], object],
    seconds: int,
) -> None:
    asked = _recording(monkeypatch, body)
    client = QuotientClient(base_url="https://quotient.test")
    start = time.time()
    get(client, False)
    with patch("time.time", return_value=start + seconds - 0.5):
        get(client, False)
    assert asked == [endpoint]
    with patch("time.time", return_value=start + seconds + 0.5):
        get(client, False)
    assert asked == [endpoint, endpoint]


def test_each_teams_service_checks_are_cached_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _recording(monkeypatch, [])
    client = QuotientClient(base_url="https://quotient.test")
    for team_number, service in [(1, "web"), (2, "web"), (1, "db"), (1, "web")]:
        client.get_service_checks(team_number, service)
    assert asked == ["/api/services/1/web", "/api/services/2/web", "/api/services/1/db"]


@pytest.mark.parametrize(("team_number", "service"), [(0, "web"), (-1, "web"), (1, "web/../db"), (1, "")])
def test_no_team_or_a_name_quotient_cannot_have_asks_nothing(
    monkeypatch: pytest.MonkeyPatch, team_number: int, service: str
) -> None:
    asked = _recording(monkeypatch, [{"Round": {"ID": 1}, "Result": True}])
    assert QuotientClient(base_url="https://quotient.test").get_service_checks(team_number, service) == []
    assert asked == []
