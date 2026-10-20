"""What another service (Authentik, Quotient) can answer: any status, and any body, JSON or not.

A reader's property test serves these answers to every request the reader makes (serving) and checks the reader
returns its documented result or raises its documented error. near(example) leans to JSON shaped like the real
one with any part wrong (a key missing, a value of another type or null, extra keys, huge numbers), and bodies(example)
adds deeply nested JSON and bytes that aren't JSON.
"""

import json
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import httpx
import httpx._config
from hypothesis import strategies as st

from core.tests.strategies import request_text

LEAVES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.sampled_from((2**31, 2**63, -(2**63) - 1, 10**30, 0, -1)),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
    request_text(40),
)
ANY_JSON = st.recursive(
    LEAVES,
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=10), inner, max_size=4)),
    max_leaves=12,
)


def near(example: Any) -> st.SearchStrategy[Any]:
    """JSON shaped like the example, with any part of it missing, null, of another type, or longer."""
    if isinstance(example, dict):
        kept = {key: st.one_of(near(value), near(value), near(value), ANY_JSON) for key, value in example.items()}
        shaped = st.fixed_dictionaries({}, optional=kept)
        whole = st.fixed_dictionaries(kept)
        extra = st.dictionaries(st.text(max_size=10), ANY_JSON, max_size=2)
        return st.one_of(whole, whole, shaped, st.tuples(whole, extra).map(lambda t: {**t[1], **t[0]}), ANY_JSON)
    if isinstance(example, list):
        items = [near(item) for item in example] or [ANY_JSON]
        return st.one_of(st.lists(st.one_of(*items), max_size=5), st.lists(st.one_of(*items, ANY_JSON), max_size=5))
    return st.one_of(st.just(example), st.just(example), LEAVES)


def deep(n: int) -> bytes:
    return ("[" * n + "]" * n).encode()


def bodies(example: Any) -> st.SearchStrategy[bytes]:
    """A body: JSON near the example (most often), any JSON, JSON nested past Python's recursion limit, or bytes that
    aren't JSON at all."""
    as_json = near(example).map(lambda value: json.dumps(value).encode())
    return st.one_of(
        as_json,
        as_json,
        as_json,
        ANY_JSON.map(lambda value: json.dumps(value).encode()),
        st.sampled_from((deep(50), deep(5000), deep(100_000))),
        st.binary(max_size=100),
        st.sampled_from((b"", b"{", b"null", b"NaN", b'{"a": 1e999}', b"<html>502 Bad Gateway</html>")),
    )


STATUSES = (200, 200, 200, 200, 201, 204, 301, 400, 401, 403, 404, 429, 500, 502, 503)


@dataclass(frozen=True)
class Answer:
    status: int
    body: bytes


def answers(example: Any) -> st.SearchStrategy[Answer]:
    return st.builds(Answer, st.sampled_from(STATUSES), bodies(example))


def answer_lists(*examples: Any) -> st.SearchStrategy[list[Answer]]:
    """The answers to a reader's requests in order, each shaped like its example."""
    return st.tuples(*(answers(example) for example in examples)).map(list)


class EndlessRequests(BaseException):
    """A reader that keeps requesting (an endless page loop). A BaseException, so a reader's own broad except
    can't swallow it."""


MAX_REQUESTS = 200
_SERVING_SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


@contextmanager
def serving(answers_: list[Answer]) -> Iterator[list[str]]:
    """Every outgoing request (httpx's transport) gets the next answer (the last one repeats, so a reader that pages on
    gets the same page again); yields the URLs requested."""
    requested: list[str] = []

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        if len(requested) >= MAX_REQUESTS:
            raise EndlessRequests(f"{MAX_REQUESTS} requests, the last {request.url}")
        answer = answers_[min(len(requested), len(answers_) - 1)]
        requested.append(str(request.url))
        return httpx.Response(answer.status, content=answer.body, request=request)

    with (
        patch.object(httpx.HTTPTransport, "handle_request", handle),
        patch.object(httpx._config, "create_ssl_context", return_value=_SERVING_SSL_CONTEXT),
    ):
        yield requested
