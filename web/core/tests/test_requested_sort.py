"""A list's ?sort= is one of its sort fields in either direction, "default" for no ordering, or else its default."""

import pytest
from django.test import RequestFactory

from core.utils import requested_sort


@pytest.mark.parametrize(
    ("sort", "expected"),
    [("name", "name"), ("-name", "-name"), ("default", ""), ("-secret", "-created_at"), ("", "-created_at")],
)
def test_requested_sort(sort: str, expected: str) -> None:
    request = RequestFactory().get("/", {"sort": sort} if sort else {})
    assert requested_sort(request, ["name", "created_at"], "-created_at") == expected
