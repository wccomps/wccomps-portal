"""Fixtures for the view property tests (views_harness)."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import transaction

from core.tests.views_harness import World, build_world, offline


@pytest.fixture(scope="module")
def module_world(django_db_setup: Any, django_db_blocker: Any) -> Iterator[World]:
    """One world per test module (building it takes most of a second): each test runs in a savepoint inside the
    module's transaction (pytest-django's db), and each example in one more (rolled_back), all rolled back."""
    from ticketing.tests.seeded_categories import ensure_seeded_categories

    with django_db_blocker.unblock(), transaction.atomic(), offline():
        ensure_seeded_categories()  # a transactional test's flush may have run on this worker (web/conftest.py)
        yield build_world()
        transaction.set_rollback(True)


@pytest.fixture
def view_world(module_world: World, db: Any) -> World:
    """The rows every URL pattern's views look up, signed-in clients for every role, and no other service."""
    return module_world
