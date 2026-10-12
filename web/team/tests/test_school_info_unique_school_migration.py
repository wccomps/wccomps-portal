"""The migration that gives each school at most one team in the live school list, on a database with rows."""

import pytest
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

UNIQUE = ("team", "0013_school_info_one_team_per_school")


def _latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def _before():
    """Team just before the migration; every other app at its latest migration that doesn't need it."""
    graph = MigrationExecutor(connection).loader.graph
    needs_it, todo = set(), [graph.node_map[UNIQUE]]
    while todo:
        migration = todo.pop()
        needs_it.add(migration.key)
        todo.extend(migration.children)
    targets = []
    for node in _latest():
        while node in needs_it:
            node = next(parent for parent in graph.node_map[node].parents if parent[0] == node[0])
        targets.append(node)
    return targets


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def old_apps():
    apps = _migrate(_before())
    yield apps
    _migrate(_latest())


def _school_info(apps, number, school):
    Team = apps.get_model("team", "Team")
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}")
    return apps.get_model("team", "SchoolInfo").objects.create(
        team=team, school=school, contact_email=f"t{number}@example.edu"
    )


def _school(apps, name):
    return apps.get_model("registration", "School").objects.create(name=name, name_key=name.casefold())


def test_two_teams_on_one_school_stop_the_migration_with_a_clear_error(old_apps):
    school = _school(old_apps, "Example University")
    _school_info(old_apps, 3, school)
    _school_info(old_apps, 7, school)

    with pytest.raises(RuntimeError, match=r"Example University.*Team 3.*Team 7"):
        _migrate(_latest())

    # The fixture migrates forward again afterwards, which needs the clash gone
    old_apps.get_model("team", "SchoolInfo").objects.filter(team__team_number=7).delete()


def test_one_team_per_school_migrates_and_is_then_enforced(old_apps):
    first = _school(old_apps, "Example University")
    _school_info(old_apps, 1, first)
    _school_info(old_apps, 2, _school(old_apps, "Other College"))

    new_apps = _migrate(_latest())

    school_info = new_apps.get_model("team", "SchoolInfo")
    team = new_apps.get_model("team", "Team").objects.create(team_number=3, team_name="Team 03")
    with pytest.raises(IntegrityError):
        school_info.objects.create(team=team, school_id=first.pk, contact_email="t3@example.edu")
