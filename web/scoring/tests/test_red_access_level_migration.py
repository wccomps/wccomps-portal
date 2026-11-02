"""The migration that folds the two access booleans into one access level keeps every finding's level."""

from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE = [("scoring", "0046_red_finding_outcome_help_and_ip_template")]
AFTER = [("scoring", "0047_redteamscore_access_level")]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def old_apps():
    apps = _migrate(BEFORE)
    yield apps
    _migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


def test_each_finding_keeps_its_highest_access(old_apps) -> None:
    old = old_apps.get_model("scoring", "RedTeamScore")
    rows = {
        "root": old.objects.create(points_per_team=Decimal(100), root_access=True, user_access=True),
        "user": old.objects.create(points_per_team=Decimal(25), user_access=True),
        "none": old.objects.create(points_per_team=Decimal(0)),
    }
    new = _migrate(AFTER).get_model("scoring", "RedTeamScore")
    assert {level: new.objects.get(pk=row.pk).access_level for level, row in rows.items()} == {
        "root": "root",
        "user": "user",
        "none": "none",
    }

    back = _migrate(BEFORE).get_model("scoring", "RedTeamScore")
    assert [(r.root_access, r.user_access) for r in back.objects.order_by("pk")] == [
        (True, False),
        (False, True),
        (False, False),
    ]
