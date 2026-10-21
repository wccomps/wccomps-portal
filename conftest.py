"""Hypothesis profiles and migration-seed restoration for every test under the repo."""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from asgiref.sync import SyncToAsync
from django.db import connections
from django.db.models.signals import post_migrate
from hypothesis import HealthCheck, settings


def _restore_migration_seeds(app_config: Any, **kwargs: Any) -> None:
    from ticketing.tests.seeded_categories import restore_migration_seeds

    restore_migration_seeds(app_config, **kwargs)


post_migrate.connect(_restore_migration_seeds, dispatch_uid="restore_migration_seeds")


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup: object) -> Iterator[None]:
    """Close connections opened on asgiref's ORM thread before pytest-django drops the test database."""
    yield
    SyncToAsync.single_thread_executor.submit(connections.close_all).result()


# Property tests take pytest fixtures once per test and roll each example back themselves
# (core.tests.strategies.rolled_back), so the function-scoped fixture check doesn't apply. Hostile inputs are
# slow and large on purpose (5000-character strings, files), so the speed and size checks don't either.
_SUPPRESSED = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large]

# ci: fixed examples, few enough that the whole suite stays fast; deep: random ones (nightly on main). Each has a
# "-views" variant with fewer, for the view property tests, which send a whole request per example.
settings.register_profile(
    "ci", max_examples=25, derandomize=True, deadline=None, database=None, suppress_health_check=_SUPPRESSED
)
settings.register_profile("deep", max_examples=2000, deadline=None, suppress_health_check=_SUPPRESSED)
settings.register_profile("ci-views", parent=settings.get_profile("ci"), max_examples=10)
settings.register_profile("deep-views", parent=settings.get_profile("deep"), max_examples=500)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
