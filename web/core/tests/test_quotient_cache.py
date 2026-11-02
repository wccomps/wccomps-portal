"""The Quotient client answers from its cache, an empty answer included; the score sync's reads always fetch."""

from collections.abc import Callable

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


def _asked_twice(monkeypatch: pytest.MonkeyPatch, body: object, get: Callable[[QuotientClient], object]) -> list[str]:
    asked: list[str] = []

    def request(self: QuotientClient, method: str, path: str, **kwargs: object) -> httpx.Response:
        asked.append(path)
        return httpx.Response(200, json=body, request=httpx.Request(method.upper(), f"https://quotient.test{path}"))

    monkeypatch.setattr(QuotientClient, "_request", request)
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
