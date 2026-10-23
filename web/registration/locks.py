from collections.abc import Iterator
from contextlib import contextmanager, suppress

from django.db import DatabaseError, connection

# Arbitrary, but fixed (and not prepare_database's): every holder asks for the same lock.
SCHOOL_LIST_LOCK_ID = 7_261_640_002


def school_list_lock() -> None:
    """Acquire the transaction-scoped advisory lock serializing school list, open-event, and packet distribution
    writes; released at commit before any email is sent. Re-entrant within one transaction."""
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
