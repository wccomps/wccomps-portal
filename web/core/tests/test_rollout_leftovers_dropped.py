"""Columns and tables kept only for rolling deploys are gone once no pod runs the release that used them."""

import pytest
from django.apps import apps
from django.db import connection

pytestmark = pytest.mark.django_db

# Columns the models no longer have but the database keeps, for pods on the release before their state-only
# removal. Drop them (RunSQL DROP COLUMN IF EXISTS; makemigrations never will) once no pod runs a release older
# than the one that removed them, then move them to test_kept_columns_are_dropped.
KEPT_COLUMNS = {
    "orange_check": {"closed_at"},  # orange_team 0008
    "registration_event": {"reminder_days", "last_reminder_sent"},  # registration 0009
    "registration_eventteamassignment": {"password_generated"},  # registration 0009
    "team_discordlink": {
        "helper_activated_at",  # team 0014
        "helper_deactivated_at",
        "helper_removal_reason",
        "is_student_helper",  # team 0015: student helpers are retired
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
    assert "last_check" not in _columns("core_competitionconfig")
    assert "max_members" not in _columns("team_team")


def test_columns_kept_for_the_rollout_are_only_in_the_database() -> None:
    """Each kept column still exists (drop it here when its migration drops it) and no model uses it."""
    model_columns = {
        model._meta.db_table: {field.column for field in model._meta.concrete_fields} for model in apps.get_models()
    }
    for table, columns in KEPT_COLUMNS.items():
        assert columns <= _columns(table), f"{table}: dropped, so take them out of KEPT_COLUMNS"
        assert not columns & model_columns[table], f"{table}: a model still has {columns & model_columns[table]}"
