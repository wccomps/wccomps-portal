from collections.abc import Iterator
from contextlib import contextmanager, suppress

from django.db import DatabaseError, connection

# Arbitrary, but fixed (and not prepare_database's): every holder asks for the same lock.
SCHOOL_LIST_LOCK_ID = 7_261_640_002


def school_list_lock() -> None:
    """Wait for the school list, then hold it until the transaction commits or rolls back.

    Taken by the registration services' writes (set_team_school, remove_team_school, clear_school_list,
    import_school_list, add_to_school_list, close_out_event, cancel_event, clear_leftover_data, delete_event,
    delete_season), by Event.open, Event.close and Season.activate, by the start of a first packet send
    (begin_distribution) and by the resend checks (check_resendable, and resend_to_team's contact write). Each takes
    it before reading the open event. It is released at commit, before any packet email goes out: emails are sent
    outside it. Re-entrant within one transaction, so an import can call set_team_school and Event.open.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])


@contextmanager
def school_list_held() -> Iterator[None]:
    """Hold the school list lock (the same lock, for the whole session) around a transaction opened inside it.

    For a REPEATABLE READ transaction: its snapshot is taken at its first statement, so a lock taken inside it would
    come after the snapshot, and a wait for the lock would leave it reading what was there before the holder
    committed. Outside any transaction only. school_list_lock() inside it returns at once.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [SCHOOL_LIST_LOCK_ID])
    try:
        yield
    finally:
        # A dropped connection released it already
        with suppress(DatabaseError), connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [SCHOOL_LIST_LOCK_ID])
