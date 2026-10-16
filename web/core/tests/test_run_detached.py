"""run_detached: a streamed operation finishes even when nobody reads the rest of its stream."""

import json
import threading
from collections.abc import Iterator

import pytest
from django.db import transaction

from core.utils import run_detached


def _join() -> None:
    for thread in threading.enumerate():
        if thread.name == "run-detached":
            thread.join(timeout=10)


def test_relays_every_line_in_order():
    assert list(run_detached(iter(["a\n", "b\n", "c\n"]))) == ["a\n", "b\n", "c\n"]


def test_operation_finishes_after_the_reader_stops():
    done = []
    reader_gone = threading.Event()

    def operation() -> Iterator[str]:
        for n in range(3):
            if n:
                reader_gone.wait(timeout=10)
            done.append(n)
            yield f"{n}\n"

    stream = run_detached(operation())
    assert next(stream) == "0\n"
    stream.close()
    reader_gone.set()
    _join()

    assert done == [0, 1, 2]


def test_a_failing_operation_ends_the_stream_with_an_error():
    def operation() -> Iterator[str]:
        yield "started\n"
        raise RuntimeError("Authentik is down")

    lines = list(run_detached(operation()))

    assert lines[0] == "started\n"
    assert json.loads(lines[-1]) == {"done": True, "success": False, "message": "Failed: Authentik is down"}


def _threads_of(lines: Iterator[str]) -> list[str]:
    return [line.strip() for line in lines]


def _which_thread() -> Iterator[str]:
    yield threading.current_thread().name + "\n"


@pytest.mark.django_db
def test_inside_an_open_transaction_it_runs_inline_and_sees_the_transaction():
    """A thread's own connection can't see the transaction's uncommitted rows (a test's), so inside one the operation
    runs in the caller's thread."""
    from team.models import Team

    Team.objects.create(team_number=7, team_name="Uncommitted")

    def operation() -> Iterator[str]:
        yield threading.current_thread().name + "\n"
        yield f"{Team.objects.filter(team_number=7).exists()}\n"

    with transaction.atomic():
        lines = _threads_of(run_detached(operation()))
    assert lines == [threading.current_thread().name, "True"]


@pytest.mark.django_db
def test_a_failing_inline_operation_ends_the_stream_with_an_error():
    def operation() -> Iterator[str]:
        yield "started\n"
        raise RuntimeError("Authentik is down")

    with transaction.atomic():
        lines = list(run_detached(operation()))
    assert json.loads(lines[-1]) == {"done": True, "success": False, "message": "Failed: Authentik is down"}


def test_outside_a_transaction_it_runs_in_its_own_thread():
    assert _threads_of(run_detached(_which_thread())) == ["run-detached"]
