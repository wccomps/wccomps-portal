"""The import re-checks the event before writing: a refused import writes nothing, and a new event is saved and
opened only when the import goes ahead."""

from datetime import date

import pytest

from registration.models import Event, Season
from registration.services import ImportBlockedError, SchoolListRow, import_school_list
from registration.tests.shown import new_event
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


def _event(season, name, active=False):
    return Event.objects.create(
        season=season, name=name, event_type="invitational", date=date(2026, 10, 3), is_active=active
    )


def _rows():
    Team.objects.create(team_number=1, team_name="Team 01")
    return [SchoolListRow(team_number=1, school_name="Example University", contact_email="c@example.edu")]


def test_the_import_refuses_under_the_lock_when_another_event_opened_first(season):
    _event(season, "First", active=True)
    second = _event(season, "Second")
    rows = _rows()

    with pytest.raises(ImportBlockedError, match=r"First \(2026\) is still open"):
        import_school_list(second, rows, "gold")

    assert not SchoolInfo.objects.exists()
    assert list(Event.objects.filter(is_active=True).values_list("name", flat=True)) == ["First"]


def test_a_new_event_is_saved_only_when_the_import_goes_ahead(season):
    _event(season, "First", active=True)
    new = Event(season=season, name="New", event_type="qualifier", date=date(2026, 11, 7), registration_open=False)
    rows = _rows()

    with pytest.raises(ImportBlockedError):
        import_school_list(new, rows, "gold")

    assert not Event.objects.filter(name="New").exists()


def test_a_new_event_is_created_and_opened(season):
    new = new_event("New", date(2026, 11, 7), "qualifier")

    counts = import_school_list(new, _rows(), "gold")

    assert counts["created"] == 1
    event = Event.objects.get(is_active=True)
    assert (event.name, event.season, event.registration_open) == ("New", season, False)


def test_opening_an_event_queues_the_teardown_of_what_earlier_events_left_in_discord(season):
    """Once, when the import opens the event: a second import into the open event opens nothing."""
    from core.models import DiscordTask

    import_school_list(new_event("New", date(2026, 11, 7), "qualifier"), _rows(), "gold")
    opened = Event.objects.get(name="New")
    import_school_list(opened, [SchoolListRow(1, "Example University", "c@example.edu")], "gold")

    tasks = [task.payload for task in DiscordTask.objects.filter(task_type="reconcile_teams")]
    assert tasks == [{"requested_by": "gold", "open_event_id": opened.pk}]


def test_a_row_for_a_team_that_no_longer_exists_blocks_the_import(season):
    event = _event(season, "Invitational")

    with pytest.raises(ImportBlockedError, match="These teams no longer exist: 9. Upload the file again."):
        import_school_list(event, [SchoolListRow(9, "Example University", "c@example.edu")], "gold")

    assert not event.is_active
    assert not SchoolInfo.objects.exists()


def test_importing_into_an_event_that_is_not_open_replaces_the_assignments_it_already_has(season):
    from registration.models import EventTeamAssignment, School

    event = _event(season, "Invitational")
    stale_team = Team.objects.create(team_number=9, team_name="Team 09")
    EventTeamAssignment.objects.create(event=event, team=stale_team, school=School.for_name("Example University"))

    import_school_list(event, _rows(), "gold")

    assert list(event.team_assignments.values_list("team__team_number", "school__name")) == [(1, "Example University")]


def test_a_queued_announcement_is_not_live_data(season):
    """It isn't a team using its number: it neither blocks re-importing the open event's list nor reads as
    leftovers."""
    from django.utils import timezone

    from core.models import QueuedAnnouncement
    from registration.services import import_problem
    from registration.stage import Stage, event_stage

    team = Team.objects.create(team_number=1, team_name="Team 01")
    QueuedAnnouncement.objects.create(team=team, message="Hi", sender_name="gold", delivered_at=timezone.now())

    assert event_stage().stage == Stage.IDLE
    assert import_problem(_event(season, "Open", active=True)) == ""


def test_opening_an_event_drops_announcements_queued_for_the_last_one(season):
    """Queued after the teardown for a team number with no channels, it would reach the new school that links it."""
    from core.models import QueuedAnnouncement

    rows = _rows()
    QueuedAnnouncement.objects.create(team=Team.objects.get(), message="Old event", sender_name="gold")

    import_school_list(_event(season, "Next"), rows, "gold")

    assert not QueuedAnnouncement.objects.exists()


def test_re_importing_the_list_keeps_a_teams_channels_while_its_school_stays(season):
    """Only a team given another school has its Discord role and category removed."""
    from core.models import DiscordTask

    event = _event(season, "Open", active=True)
    rows = _rows()
    Team.objects.create(team_number=2, team_name="Team 02")
    rows.append(SchoolListRow(team_number=2, school_name="Other College", contact_email="c@example.edu"))
    import_school_list(event, rows, "gold")
    Team.objects.update(discord_role_id=5, discord_category_id=6)

    rows[1] = SchoolListRow(team_number=2, school_name="Third College", contact_email="c@example.edu")
    import_school_list(event, rows, "gold")

    marked = Team.objects.filter(channels_need_clearing=True).values_list("team_number", flat=True)
    assert list(marked) == [2]
    assert list(DiscordTask.objects.values_list("payload__team_number", flat=True)) == [2]


def test_re_importing_the_list_drops_the_announcements_queued_for_a_team_given_another_school(season):
    from core.models import QueuedAnnouncement

    event = _event(season, "Open", active=True)
    rows = _rows()
    Team.objects.create(team_number=2, team_name="Team 02")
    rows.append(SchoolListRow(team_number=2, school_name="Other College", contact_email="c@example.edu"))
    import_school_list(event, rows, "gold")
    for team in Team.objects.all():
        QueuedAnnouncement.objects.create(team=team, message="Before", sender_name="gold")

    rows[1] = SchoolListRow(team_number=2, school_name="Third College", contact_email="c@example.edu")
    import_school_list(event, rows, "gold")

    assert list(QueuedAnnouncement.objects.values_list("team__team_number", flat=True)) == [1]
