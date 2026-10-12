"""The migration that links each SchoolInfo to its School, on a database that already has rows."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

LINK = ("team", "0012_school_info_school")


def _latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def _before():
    """Team just before the link migration; every other app at its latest migration that doesn't need it."""
    graph = MigrationExecutor(connection).loader.graph
    needs_link, todo = set(), [graph.node_map[LINK]]
    while todo:
        migration = todo.pop()
        needs_link.add(migration.key)
        todo.extend(migration.children)
    targets = []
    for node in _latest():
        while node in needs_link:
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


def _school_info(apps, number, school_name):
    Team = apps.get_model("team", "Team")
    SchoolInfo = apps.get_model("team", "SchoolInfo")
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}")
    return SchoolInfo.objects.create(team=team, school_name=school_name, contact_email=f"t{number}@example.edu")


def test_each_school_info_links_to_the_school_of_its_name(old_apps):
    first = _school_info(old_apps, 1, "Example University")
    second = _school_info(old_apps, 2, "  Other   College ")

    new_apps = _migrate(_latest())

    school_info = new_apps.get_model("team", "SchoolInfo")
    assert school_info.objects.get(pk=first.pk).school.name == "Example University"
    assert school_info.objects.get(pk=second.pk).school.name == "Other College"
    assert school_info.objects.get(pk=second.pk).school.name_key == "other college"


def test_names_differing_only_by_case_and_spacing_share_one_school(old_apps):
    first = _school_info(old_apps, 1, "Example University")
    second = _school_info(old_apps, 2, "example  university")

    new_apps = _migrate(_latest())

    school_info = new_apps.get_model("team", "SchoolInfo")
    assert school_info.objects.get(pk=first.pk).school_id == school_info.objects.get(pk=second.pk).school_id
    assert new_apps.get_model("registration", "School").objects.count() == 1


def test_an_existing_school_is_reused(old_apps):
    old_apps.get_model("registration", "School").objects.create(
        name="Example University", name_key="example university"
    )
    info = _school_info(old_apps, 1, "EXAMPLE University")

    new_apps = _migrate(_latest())

    assert new_apps.get_model("team", "SchoolInfo").objects.get(pk=info.pk).school.name == "Example University"
    assert new_apps.get_model("registration", "School").objects.count() == 1


def test_migrating_back_restores_the_name():
    _migrate(_latest())
    from registration.models import School

    from team.models import SchoolInfo, Team

    team = Team.objects.create(team_number=1, team_name="Team 01")
    SchoolInfo.objects.create(team=team, school=School.for_name("Example University"), contact_email="a@example.edu")

    try:
        old = _migrate(_before())
        assert old.get_model("team", "SchoolInfo").objects.get(team__team_number=1).school_name == "Example University"
    finally:
        _migrate(_latest())
