"""The school migration links existing assignments to schools on a database that already has rows."""

from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)


def _latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def _before():
    """Registration just before the school migration; every other app at its latest migration that doesn't need it."""
    graph = MigrationExecutor(connection).loader.graph
    needs_school, todo = set(), [graph.node_map[("registration", "0005_school")]]
    while todo:
        migration = todo.pop()
        needs_school.add(migration.key)
        todo.extend(migration.children)
    targets = [("registration", "0004_registration_rate_limit")]
    for node in _latest():
        if node[0] == "registration":
            continue
        while node in needs_school:
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


def _assignment(apps, event, number, school_name):
    Team = apps.get_model("team", "Team")
    TeamRegistration = apps.get_model("registration", "TeamRegistration")
    EventTeamAssignment = apps.get_model("registration", "EventTeamAssignment")
    team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}")
    registration = TeamRegistration.objects.create(school_name=school_name, status="approved")
    return EventTeamAssignment.objects.create(event=event, team=team, registration=registration)


def _event(apps):
    Season = apps.get_model("registration", "Season")
    Event = apps.get_model("registration", "Event")
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3))


def test_existing_assignments_get_their_registrations_school(old_apps):
    event = _event(old_apps)
    first = _assignment(old_apps, event, 1, "Example University")
    second = _assignment(old_apps, event, 2, "  Other   College ")

    new_apps = _migrate(_latest())

    assignment_model = new_apps.get_model("registration", "EventTeamAssignment")
    assert assignment_model.objects.get(pk=first.pk).school.name == "Example University"
    assert assignment_model.objects.get(pk=second.pk).school.name == "Other College"
    assert (
        assignment_model.objects.get(pk=second.pk).registration.school
        == assignment_model.objects.get(pk=second.pk).school
    )


def test_two_registrations_of_one_school_in_an_event_stop_the_migration_with_a_clear_error(old_apps):
    event = _event(old_apps)
    _assignment(old_apps, event, 1, "Example University")
    _assignment(old_apps, event, 2, "example  university")

    with pytest.raises(RuntimeError, match=r"(?i)example university.*Rename one registration"):
        _migrate(_latest())

    # The fixture migrates forward again afterwards, which needs the clash gone
    old_apps.get_model("registration", "EventTeamAssignment").objects.filter(team__team_number=2).delete()


def test_reversing_with_imported_assignments_stops_with_a_clear_error():
    _migrate(_latest())
    from registration.models import EventTeamAssignment, School, Season
    from team.models import Team

    season = Season.objects.create(name="2026", year=2026, is_active=False)
    event = season.events.create(name="Invitational", event_type="invitational", date=date(2026, 10, 3))
    team = Team.objects.create(team_number=1, team_name="Team 01")
    EventTeamAssignment.objects.create(event=event, team=team, school=School.for_name("Example University"))

    try:
        with pytest.raises(RuntimeError, match=r"Imported assignments have no registration; reverse 0005 only after"):
            _migrate(_before())
    finally:
        _migrate(_latest())
