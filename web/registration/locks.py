from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection

from core.utils import advisory_lock

# Arbitrary, but fixed (and not prepare_database's): every holder asks for the same lock.
SCHOOL_LIST_LOCK_ID = 7_261_640_002


def school_list_lock() -> None:
    """Serialize roster and event writes with the local commit phase of service score imports."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])


@contextmanager
def school_list_held() -> Iterator[None]:
    """Hold the school list lock (the same lock, for the whole session) around a transaction opened inside it.

    For a REPEATABLE READ transaction: its snapshot is taken at its first statement, so a lock taken inside it would
    come after the snapshot, and a wait for the lock would leave it reading what was there before the holder
    committed. Outside any transaction only. school_list_lock() inside it returns at once.
    """
    with advisory_lock(SCHOOL_LIST_LOCK_ID, wait=True):
        yield
