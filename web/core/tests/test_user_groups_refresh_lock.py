"""The bot's periodic refresh skips while another refresh holds the lock; a competition start or stop waits for it."""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from django.db import connection, transaction

from core.services.user_groups import REFRESH_LOCK, refresh_user_groups, refresh_user_groups_unless_running

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def _lock_held_elsewhere(for_seconds: float = 10) -> Iterator[None]:
    """Another connection holds the refresh lock for up to for_seconds, or until the block ends."""
    holding = threading.Event()
    release = threading.Event()

    def hold_the_lock() -> None:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [REFRESH_LOCK])
                holding.set()
                release.wait(for_seconds)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    try:
        assert holding.wait(10)
        yield
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()


def test_the_periodic_refresh_skips_while_another_runs():
    manager = MagicMock()
    with _lock_held_elsewhere():
        assert refresh_user_groups_unless_running(manager) is None
    manager.list_all_groups.assert_not_called()


def test_a_waiting_refresh_outlasts_the_statement_timeout():
    """Start and Stop wait for a slow periodic refresh instead of failing at the connection's statement timeout."""
    manager = MagicMock()
    manager.list_all_groups.return_value = []
    manager.list_all_users.return_value = [{"uid": "u", "is_active": True, "groups": []}]
    with connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '300ms'")
    try:
        with _lock_held_elsewhere(for_seconds=1):
            started = time.monotonic()
            refresh_user_groups(manager)
        assert time.monotonic() - started >= 0.5
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET statement_timeout")
