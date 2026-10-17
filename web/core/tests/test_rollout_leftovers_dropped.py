"""Columns and tables kept only for rolling deploys are gone once no pod runs the release that used them."""

import pytest
from django.apps import apps
from django.db import connection

pytestmark = pytest.mark.django_db

# Columns the models no longer have but the database keeps, for pods on the release before their state-only
# removal. Drop them (RunSQL DROP COLUMN IF EXISTS; makemigrations never will) once no pod runs a release older
# than the one that removed them, then move them to test_kept_columns_are_dropped.
KEPT_COLUMNS: dict[str, set[str]] = {}

# Columns a migration has dropped, and the migration that dropped them.
DROPPED_COLUMNS = {
    "core_competitionconfig": {"last_check"},  # core 0015
    "team_team": {"max_members"},  # team 0008
    "orange_check": {"closed_at"},  # orange_team 0010
    "registration_event": {"reminder_days", "last_reminder_sent"},  # registration 0012
    "registration_eventteamassignment": {"password_generated"},  # registration 0012
    "team_discordlink": {  # team 0016
        "helper_activated_at",
        "helper_deactivated_at",
        "helper_removal_reason",
        "is_student_helper",
        "helper_role_name",
        "helper_role_id",
    },
}


def _columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {column.name for column in connection.introspection.get_table_description(cursor, table)}


def test_kept_tables_are_dropped() -> None:
    tables = set(connection.introspection.table_names())
    assert not tables & {"final_score", "core_dashboardupdate"}


def test_kept_columns_are_dropped() -> None:
    for table, columns in DROPPED_COLUMNS.items():
        assert not columns & _columns(table), f"{table} still has {columns & _columns(table)}"


def test_columns_kept_for_the_rollout_are_only_in_the_database() -> None:
    """Each kept column still exists (drop it here when its migration drops it) and no model uses it."""
    model_columns = {
        model._meta.db_table: {field.column for field in model._meta.concrete_fields} for model in apps.get_models()
    }
    for table, columns in KEPT_COLUMNS.items():
        assert columns <= _columns(table), f"{table}: dropped, so take them out of KEPT_COLUMNS"
        assert not columns & model_columns[table], f"{table}: a model still has {columns & model_columns[table]}"
