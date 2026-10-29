"""The Quotient client answers from its cache until the entry expires, an empty answer included."""

from collections.abc import Callable

import httpx
import pytest
from django.core.cache import cache
from quotient.client import QuotientClient, QuotientInfrastructure

GETTERS: list[tuple[str, object, Callable[[QuotientClient], object]]] = [
    ("/api/metadata", {"boxes": []}, lambda c: c.get_infrastructure()),
    ("/api/injects", [], lambda c: c.get_injects()),
    ("/api/engine/export/scores", [], lambda c: c.get_service_export()),
    ("/api/graphs/uptimes", {"series": []}, lambda c: c.get_uptimes()),
    ("/api/services/1/web", [], lambda c: c.get_service_checks(1, "web")),
]


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.parametrize(("endpoint", "body", "get"), GETTERS, ids=[g[0] for g in GETTERS])
def test_an_empty_answer_is_cached_like_any_other(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, body: object, get: Callable[[QuotientClient], object]
) -> None:
    asked: list[str] = []

    def request(self: QuotientClient, method: str, path: str, **kwargs: object) -> httpx.Response:
        asked.append(path)
        return httpx.Response(200, json=body, request=httpx.Request(method.upper(), f"https://quotient.test{path}"))

    monkeypatch.setattr(QuotientClient, "_request", request)
    client = QuotientClient(base_url="https://quotient.test")
    first = get(client)
    second = get(client)
    assert first == second
    assert first == QuotientInfrastructure(boxes=[]) or first == []
    assert asked == [endpoint]
