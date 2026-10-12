"""The one-open-event migration stops with a clear error, naming the events, when more than one is active."""

from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

CONSTRAINT = ("registration", "0006_one_open_event")


def _latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def _before():
    """Registration just before the constraint; every other app at its latest migration that doesn't need it."""
    graph = MigrationExecutor(connection).loader.graph
    needs_it, todo = set(), [graph.node_map[CONSTRAINT]]
    while todo:
        migration = todo.pop()
        needs_it.add(migration.key)
        todo.extend(migration.children)
    targets = [("registration", "0005_school")]
    for node in _latest():
        if node[0] == "registration":
            continue
        while node in needs_it:
            node = next(parent for parent in graph.node_map[node].parents if parent[0] == node[0])
        targets.append(node)
    return targets


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


def test_two_active_events_stop_the_migration_naming_them():
    old_apps = _migrate(_before())
    Season = old_apps.get_model("registration", "Season")
    Event = old_apps.get_model("registration", "Event")
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    for name, event_type in [("Invitational", "invitational"), ("Qualifier", "qualifier")]:
        Event.objects.create(season=season, name=name, event_type=event_type, date=date(2026, 10, 3), is_active=True)

    try:
        with pytest.raises(RuntimeError, match=r"Invitational.*Qualifier.*Deactivate all but one"):
            _migrate(_latest())
    finally:
        Event.objects.filter(name="Qualifier").update(is_active=False)
        _migrate(_latest())
