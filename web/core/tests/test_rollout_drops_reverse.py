"""The migrations that drop the rollout's kept columns roll back to the state the keeping migrations left, and
forward again; behind a long query each gives up on its lock timeout instead of waiting."""

import psycopg2
import pytest
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE_DROPS = {
    "orange_team": "0009_point_limits",
    "registration": "0011_event_clash_message",
    "team": "0015_discordlink_forget_student_helper",
}

# (table, column): (nullable, default) as the keeping migrations left them
KEPT = {
    ("orange_check", "closed_at"): (True, None),
    ("registration_event", "reminder_days"): (False, "'[]'::jsonb"),
    ("registration_event", "last_reminder_sent"): (True, None),
    ("registration_eventteamassignment", "password_generated"): (False, "''::character varying"),
    ("team_discordlink", "helper_activated_at"): (True, None),
    ("team_discordlink", "helper_deactivated_at"): (True, None),
    ("team_discordlink", "helper_removal_reason"): (False, "''::text"),
    ("team_discordlink", "is_student_helper"): (False, "false"),
    ("team_discordlink", "helper_role_name"): (False, "''::character varying"),
    ("team_discordlink", "helper_role_id"): (True, None),
}
HELPER_INDEX = "team_discordlink_is_student_helper_5496ec0c"

# Each drop, in order, and the table it alters
DROPS = [
    (("orange_team", "0010_drop_orange_check_closed_at"), "orange_check"),
    (("registration", "0012_drop_event_reminder_columns"), "registration_event"),
    (("registration", "0013_drop_eventteamassignment_password_generated"), "registration_eventteamassignment"),
    (("team", "0016_drop_discordlink_helper_columns"), "team_discordlink"),
]


def _migrate(targets: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


def _latest() -> list[tuple[str, str]]:
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


def _kept_columns() -> dict[tuple[str, str], tuple[bool, str | None]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name, column_name, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
        return {
            (table, column): (nullable == "YES", default)
            for table, column, nullable, default in cursor.fetchall()
            if (table, column) in KEPT
        }


def _helper_index() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s", [HELPER_INDEX]
        )
        return [row[0] for row in cursor.fetchall()]


def _fails_on_the_lock_timeout(target: tuple[str, str], table: str) -> None:
    """Migrating to target while another transaction reads the table gives up on the lock timeout."""
    db = connection.settings_dict
    other = psycopg2.connect(
        host=db["HOST"], port=db["PORT"], user=db["USER"], password=db["PASSWORD"], dbname=db["NAME"]
    )
    try:
        other.cursor().execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
        with pytest.raises(OperationalError) as raised:
            _migrate([target])
        assert isinstance(raised.value.__cause__, psycopg2.errors.LockNotAvailable), raised.value
    finally:
        other.close()


def test_the_drops_roll_back_to_the_kept_columns_and_forward_again() -> None:
    latest = _latest()
    try:
        _migrate([(app, BEFORE_DROPS.get(app, name)) for app, name in latest])
        assert _kept_columns() == KEPT
        assert _helper_index() == [
            f"CREATE INDEX {HELPER_INDEX} ON public.team_discordlink USING btree (is_student_helper)"
        ]
        for target, table in DROPS:
            _fails_on_the_lock_timeout(target, table)
            assert table in {t for t, _ in _kept_columns()}
    finally:
        _migrate(latest)
    assert _kept_columns() == {}
    assert _helper_index() == []
