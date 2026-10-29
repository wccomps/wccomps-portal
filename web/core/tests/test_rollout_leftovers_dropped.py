"""Every table and column in the database belongs to a model.

A state-only removal (SeparateDatabaseAndState) keeps its column or table for pods on the previous release
during a rolling deploy. List it in EXCEPTIONS with the migration that removed it until a later migration drops
it (RunSQL DROP ... IF EXISTS; makemigrations never will); an exception that matches nothing fails, so the list
empties as the drops land.
"""

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from core.tests.guards import fail_with

pytestmark = pytest.mark.django_db

# "table" or "table.column": why the database keeps it without a model
EXCEPTIONS: dict[str, str] = {
    "red_team_finding.root_access": "scoring 0047 folded it into access_level; the drop must first rerun 0047's "
    "_to_level for rows with access_level 'none' and an old boolean set (written by the old release mid-rollout)",
    "red_team_finding.user_access": "scoring 0047 folded it into access_level; see root_access",
    "scoring_template.orange_max": "scoring 0048 forgot it: the scoring sums the orange checks instead; when it "
    "is dropped, orange_team.models.MAX_TOTAL_POINTS (kept for the old release's writes) goes, and only the per-check "
    "MAX_POINTS remains",
}


def _model_columns() -> dict[str, set[str]]:
    """Each table the models (m2m through tables and Django's migration record included) use, with its columns."""
    tables: dict[str, set[str]] = {}
    for model in [*apps.get_models(include_auto_created=True), MigrationRecorder.Migration]:
        tables.setdefault(model._meta.db_table, set()).update(
            field.column for field in model._meta.local_concrete_fields
        )
    return tables


def test_every_table_and_column_belongs_to_a_model() -> None:
    expected = _model_columns()
    found: dict[str, str] = {}
    with connection.cursor() as cursor:
        for table in connection.introspection.table_names(cursor):
            if table not in expected:
                found[table] = "no model has this table"
                continue
            for column in connection.introspection.get_table_description(cursor, table):
                if column.name not in expected[table]:
                    found[f"{table}.{column.name}"] = "no model field has this column"
    fail_with("Database tables and columns no model has", found, EXCEPTIONS)
