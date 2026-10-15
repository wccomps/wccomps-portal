"""Migration 0010 makes School.name_key unlimited; going back refuses rather than cut longer keys to 255."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = ("registration", "0009_forget_unused_event_fields")


def _migrate(targets: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


def _latest() -> list[tuple[str, str]]:
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def test_reversing_with_a_key_longer_than_255_stops_and_names_the_school() -> None:
    from registration.models import School

    School.objects.create(name="ß" * 200, name_key="ss" * 200)
    try:
        with pytest.raises(RuntimeError, match=r"longer than 255"):
            _migrate([BEFORE])
        assert School.objects.get(name="ß" * 200).name_key == "ss" * 200
    finally:
        School.objects.filter(name="ß" * 200).delete()
        _migrate(_latest())


def test_reversing_with_short_keys_goes_back_and_forward() -> None:
    from registration.models import School

    School.objects.create(name="Short", name_key="short")
    try:
        _migrate([BEFORE])
        _migrate(_latest())
        assert School.objects.get(name="Short").name_key == "short"
    finally:
        School.objects.filter(name="Short").delete()
        _migrate(_latest())
