"""Every view property row (views_harness.ViewCase), by the module that holds it: the one index the coverage guard,
the browser registry and the nav tests read."""

from collections.abc import Iterator
from functools import cache

from core.tests import (
    test_properties_views_admin_site,
    test_properties_views_core,
    test_properties_views_orange,
    test_properties_views_packets,
    test_properties_views_registration,
    test_properties_views_scoring,
    test_properties_views_ticketing,
)
from core.tests.views_harness import ViewCase

# Each runs its properties for every row of its CASES
VIEW_MODULES = (
    test_properties_views_admin_site,
    test_properties_views_core,
    test_properties_views_orange,
    test_properties_views_packets,
    test_properties_views_registration,
    test_properties_views_scoring,
    test_properties_views_ticketing,
)


def rows() -> Iterator[ViewCase]:
    for module in VIEW_MODULES:
        yield from module.CASES


@cache
def allowed_roles(url_name: str) -> frozenset[str]:
    """The roles the row driving this URL name lets in (a row drives its name and its also names)."""
    for case in rows():
        if url_name in (case.name, *case.also):
            return case.allowed
    raise KeyError(f"no view property row drives {url_name}")
