"""Unlinking a team and the Close Out wipe lock the team rows before the links, so neither order deadlocks. The wipe
runs at REPEATABLE READ, as Close Out runs it, where a row changed under it is a refusal (40001), never a deadlock.
The team lock leaves rows referencing the team writable, and linking waits for it."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import connection, transaction
from registration.services import SERIALIZATION_FAILURE

from core import competition_utils
from core.auth_utils import sign_out_everywhere
from core.competition_utils import wipe_competition_data
from core.services import linking
from core.services.linking import SIGNED_OUT, LinkResult, execute_link, unlink_teams
from team.models import DiscordLink, Team, TeamQuerySet

pytestmark = pytest.mark.django_db(transaction=True)

# Long enough for the other transaction to reach its lock wait; PostgreSQL detects a deadlock after 1 s
OVERLAP = 1.5


def _in_thread(
    work: Callable[[], object], errors: list[BaseException], *, repeatable_read: bool = False
) -> threading.Thread:
    def run() -> None:
        try:
            with transaction.atomic():
                if repeatable_read:
                    with connection.cursor() as cursor:
                        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                work()
        except BaseException as e:
            errors.append(e)
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _not_refusals(errors: list[BaseException]) -> list[BaseException]:
    """The errors other than a serialization failure, which Close Out answers as a refusal."""
    return [e for e in errors if getattr(e.__cause__, "pgcode", None) != SERIALIZATION_FAILURE]


@pytest.fixture
def team() -> Team:
    team = Team.objects.create(team_number=6, team_name="Team 06", ticket_counter=3)
    member = User.objects.create(username="member06")
    DiscordLink.objects.create(user=member, discord_id=606, discord_username="m", team=team, is_active=True)
    return team


def _unlink(team: Team) -> Callable[[], object]:
    return lambda: unlink_teams(Team.objects.filter(pk=team.pk), "gold-admin", "team_reset")


def test_a_wipe_started_while_an_unlink_holds_the_team_waits_for_it(team):
    errors: list[BaseException] = []
    team_locked = threading.Event()
    original = linking.unlink_discord_links

    def unlink_after_a_pause(*args, **kwargs):
        team_locked.set()
        time.sleep(OVERLAP)
        return original(*args, **kwargs)

    with patch.object(linking, "unlink_discord_links", unlink_after_a_pause):
        unlinking = _in_thread(_unlink(team), errors)
        assert team_locked.wait(10)
        wiping = _in_thread(wipe_competition_data, errors, repeatable_read=True)
        unlinking.join(30)
        wiping.join(30)

    assert not unlinking.is_alive() and not wiping.is_alive()
    assert _not_refusals(errors) == []


def test_an_unlink_started_while_a_wipe_holds_the_links_waits_for_it(team):
    errors: list[BaseException] = []
    links_deleted = threading.Event()
    original = competition_utils._used_ticket_counters

    def counters_after_a_pause():
        links_deleted.set()
        time.sleep(OVERLAP)
        return original()

    with patch.object(competition_utils, "_used_ticket_counters", counters_after_a_pause):
        wiping = _in_thread(wipe_competition_data, errors, repeatable_read=True)
        assert links_deleted.wait(10)
        unlinking = _in_thread(_unlink(team), errors)
        wiping.join(30)
        unlinking.join(30)

    assert not unlinking.is_alive() and not wiping.is_alive()
    assert _not_refusals(errors) == []


def test_a_feedback_edit_holding_a_score_does_not_deadlock_the_wipe(team):
    """The feedback views lock the score and read its team; locking the team too would deadlock the wipe, which
    holds the team rows when it reaches the scores."""
    from scoring.models import InjectScore
    from scoring.views.injects import locked_score

    score = InjectScore.objects.create(team=team, inject_id="1", inject_name="Inject", points_awarded=5)
    errors: list[BaseException] = []
    teams_locked = threading.Event()
    original = TeamQuerySet.lock

    def lock_then_pause(queryset):
        locked = original(queryset)
        teams_locked.set()
        time.sleep(OVERLAP)
        return locked

    def edit_feedback() -> None:
        locked_score(score.pk)
        time.sleep(OVERLAP / 2)

    with patch.object(TeamQuerySet, "lock", lock_then_pause):
        wiping = _in_thread(wipe_competition_data, errors, repeatable_read=True)
        assert teams_locked.wait(10)
        editing = _in_thread(edit_feedback, errors)
        wiping.join(30)
        editing.join(30)

    assert not wiping.is_alive() and not editing.is_alive()
    assert _not_refusals(errors) == []


@contextmanager
def _another_transaction_holding(team: Team, signing_out: User | None = None) -> Iterator[None]:
    """Another transaction holds the team's lock until the block ends, as an unlink or the wipe does. With signing_out,
    it first signs that account out everywhere, as a credentials reset does before its unlink."""
    errors: list[BaseException] = []
    team_locked = threading.Event()
    release = threading.Event()

    def hold() -> None:
        if signing_out is not None:
            sign_out_everywhere(User.objects.get(pk=signing_out.pk))
        assert Team.objects.filter(pk=team.pk).lock() == [team.pk]
        team_locked.set()
        release.wait(10)

    holding = _in_thread(hold, errors)
    try:
        assert team_locked.wait(10)
        yield
    finally:
        release.set()
        holding.join(30)
    assert errors == []


def test_a_locked_team_still_takes_rows_that_reference_it(team):
    """The team lock is FOR NO KEY UPDATE, so while an unlink or the wipe holds a team, rows referencing it stay
    writable: an insert's foreign key check needs only a key share lock on the team. The row here is a link inserted
    directly; a link made through execute_link takes the team lock itself and waits."""
    joiner = User.objects.create(username="joiner06")

    with _another_transaction_holding(team), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
        DiscordLink.objects.create(user=joiner, discord_id=607, discord_username="j", team=team, is_active=True)

    assert DiscordLink.objects.filter(team=team, is_active=True).count() == 2


def test_a_link_confirmed_during_a_reset_waits_for_it_and_links_nothing(team):
    """A credentials reset signs the account out and unlinks its team under the team lock. A link confirmed meanwhile
    by a session of that account waits for the lock, then finds the session signed out and links nothing, so no link
    outlasts the reset."""
    account = User.objects.create(username="team06")
    errors: list[BaseException] = []
    results: list[LinkResult | None] = []

    with _another_transaction_holding(team, signing_out=account):
        linker = _in_thread(lambda: results.append(execute_link(607, "j", account, team)), errors)
        time.sleep(OVERLAP)
        waited = linker.is_alive()
    linker.join(30)

    assert waited
    assert (errors, results) == ([], [SIGNED_OUT])
    assert not DiscordLink.objects.filter(discord_id=607).exists()
