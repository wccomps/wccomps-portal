"""Editing a team's school info by hand assigns the team to the active event, as the CSV import does."""

from datetime import date
from unittest.mock import patch

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from registration.models import Event, EventTeamAssignment, School, Season, TeamRegistration

from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


def _edit(client, team_number, school_name, change="rename", shown=None):
    """Posted from a page that shows the team's school now, unless shown gives (id, name key) of another."""
    if shown is None:
        shown = (
            SchoolInfo.objects.filter(team__team_number=team_number)
            .values_list("school_id", "school__name_key")
            .first()
        ) or ("", "")
    return client.post(
        reverse("school_info_edit", kwargs={"team_number": team_number}),
        {
            "school_name": school_name,
            "contact_email": "coach@example.edu",
            "change": change,
            "shown_school": shown[0],
            "shown_key": shown[1],
        },
    )


def test_new_school_info_assigns_the_team_to_the_active_event(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")

    _edit(client, 4, "Example University")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert assignment.team == team
    assert assignment.school.name == "Example University"


def test_renaming_an_assigned_teams_school_moves_its_assignment(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    EventTeamAssignment.objects.create(
        event=event, team=team, school=School.for_name("Example University"), registration=registration
    )

    _edit(client, 4, "Other College")

    assert EventTeamAssignment.objects.get(event=event).school.name == "Other College"
    assert SchoolInfo.objects.get(team=team).school_name == "Other College"


def test_changing_an_assigned_teams_school_unlinks_the_old_schools_registration(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    school = School.for_name("Example University")
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved", school=school)
    EventTeamAssignment.objects.create(event=event, team=team, school=school, registration=registration)

    _edit(client, 4, "Other College")

    registration.refresh_from_db()
    assert registration.school == school
    assert EventTeamAssignment.objects.get(event=event).registration is None


def test_spelling_change_keeps_the_school_and_updates_its_name(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    school = School.for_name("Example University")
    EventTeamAssignment.objects.create(event=event, team=team, school=school)

    _edit(client, 4, "example  university")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert assignment.school_id == school.pk
    assert assignment.school.name == "example university"


def test_renaming_to_a_school_another_team_holds_is_refused(client, event):
    other = Team.objects.create(team_number=2, team_name="Team 02")
    team = Team.objects.create(team_number=4, team_name="Team 04")
    EventTeamAssignment.objects.create(event=event, team=other, school=School.for_name("Other College"))
    mine = EventTeamAssignment.objects.create(event=event, team=team, school=School.for_name("Example University"))

    response = _edit(client, 4, "Other College")

    assert b"already has a team" in response.content
    mine.refresh_from_db()
    assert mine.school.name == "Example University"


def test_school_with_a_team_in_the_event_is_refused(client, event):
    other = Team.objects.create(team_number=2, team_name="Team 02")
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    EventTeamAssignment.objects.create(
        event=event, team=other, school=School.for_name("Example University"), registration=registration
    )
    Team.objects.create(team_number=4, team_name="Team 04")

    response = _edit(client, 4, "Example University")

    assert b"already has a team" in response.content
    assert not SchoolInfo.objects.filter(team__team_number=4).exists()
    assert EventTeamAssignment.objects.count() == 1


def test_no_active_event_saves_school_info_only(client):
    Team.objects.create(team_number=4, team_name="Team 04")

    _edit(client, 4, "Example University")

    assert SchoolInfo.objects.filter(team__team_number=4).exists()
    assert not EventTeamAssignment.objects.exists()


def test_school_list_shows_the_secondary_email(client):
    team = Team.objects.create(team_number=9, team_name="Team 09")
    SchoolInfo.objects.create(
        team=team,
        school=School.for_name("Example U"),
        contact_email="captain@example.edu",
        secondary_email="coach@example.edu",
    )

    response = client.get(reverse("school_info"))

    assert b"coach@example.edu" in response.content


def test_editing_a_school_moves_the_live_name_and_the_assignment_together(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    school = School.for_name("Example University")
    EventTeamAssignment.objects.create(event=event, team=team, school=school)
    SchoolInfo.objects.create(team=team, school=school, contact_email="coach@example.edu")

    _edit(client, 4, "Other College")

    info = SchoolInfo.objects.get(team=team)
    assert info.school == EventTeamAssignment.objects.get(event=event).school
    assert info.school_name == "Other College"


def test_a_refused_school_change_leaves_the_live_name_alone(client, event):
    other = Team.objects.create(team_number=2, team_name="Team 02")
    team = Team.objects.create(team_number=4, team_name="Team 04")
    EventTeamAssignment.objects.create(event=event, team=other, school=School.for_name("Other College"))
    school = School.for_name("Example University")
    EventTeamAssignment.objects.create(event=event, team=team, school=school)
    SchoolInfo.objects.create(team=team, school=school, contact_email="coach@example.edu")

    response = _edit(client, 4, "Other College", change="new_school")

    assert b"already has a team" in response.content
    assert b'value="Other College"' in response.content
    assert SchoolInfo.objects.get(team=team).school_name == "Example University"


def test_no_open_event_files_the_school_info_under_the_named_school(client):
    Team.objects.create(team_number=4, team_name="Team 04")

    _edit(client, 4, "  Example   University ")

    assert SchoolInfo.objects.get(team__team_number=4).school == School.objects.get(name_key="example university")


def test_the_export_shows_the_school_name(client):
    team = Team.objects.create(team_number=9, team_name="Team 09")
    SchoolInfo.objects.create(team=team, school=School.for_name("Example U"), contact_email="captain@example.edu")

    response = client.get(reverse("school_info_export"))

    assert "9,Team 09,Example U,captain@example.edu" in response.content.decode()


def test_two_teams_cannot_share_a_school_with_no_open_event(client):
    Team.objects.create(team_number=2, team_name="Team 02")
    Team.objects.create(team_number=4, team_name="Team 04")
    _edit(client, 2, "Example University")

    response = _edit(client, 4, "example university")

    assert b"already has a team: Team 2" in response.content
    assert not SchoolInfo.objects.filter(team__team_number=4).exists()


def test_an_edit_with_no_open_event_then_an_import_leaves_the_list_and_the_event_in_step(client):
    from registration.services import SchoolListRow, import_school_list

    team = Team.objects.create(team_number=4, team_name="Team 04")
    other = Team.objects.create(team_number=5, team_name="Team 05")
    _edit(client, 4, "Example University")
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3))

    import_school_list(
        event, [SchoolListRow(team_number=5, school_name="Other College", contact_email="o@example.edu")], "gold"
    )

    live = set(SchoolInfo.objects.values_list("team", "school"))
    assert live == set(EventTeamAssignment.objects.filter(event=event).values_list("team", "school"))
    assert live == {(other.pk, School.objects.get(name="Other College").pk)}
    assert team.pk not in {pk for pk, _ in live}


def test_page_error_shows_once_not_under_each_form_field(client, event):
    Team.objects.create(team_number=1, team_name="Team 01", is_active=True)

    response = client.post(reverse("school_info_edit", args=[1]), {"school_name": "", "contact_email": "bad"})

    content = response.content.decode()
    assert "This field is required" in content
    assert content.count("This field is required") == 1


def _reset(password: str | None = "new-pass", error=""):  # noqa: S107
    from core.authentik_utils import CredentialReset

    return CredentialReset(password=password, error=error, sessions_found=1)


@pytest.fixture
def linked_team():
    from django.contrib.auth.models import User
    from registration.services import set_team_school

    from team.models import DiscordLink

    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="coach@example.edu")
    Team.objects.filter(pk=team.pk).update(discord_role_id=5, discord_category_id=6)
    user = User.objects.create(username="member04")
    DiscordLink.objects.create(discord_id=44, discord_username="m", user=user, team=team, is_active=True)
    return team


def test_a_changed_name_asks_whether_it_is_a_rename_or_a_different_school(client, linked_team):
    """The page keeps what was typed, so picking an answer and saving again saves it."""
    from core.views import SAY_RENAME_OR_NEW

    response = _edit(client, 4, "Other College", change="")

    page = response.content.decode()
    assert SAY_RENAME_OR_NEW in page
    assert 'value="Other College"' in page
    assert SchoolInfo.objects.get(team=linked_team).school_name == "Example University"


def test_a_rename_keeps_the_members_and_channels(client, linked_team):
    from core.models import DiscordTask
    from team.models import DiscordLink

    _edit(client, 4, "Example State University", change="rename")

    assert SchoolInfo.objects.get(team=linked_team).school_name == "Example State University"
    assert DiscordLink.objects.get(discord_id=44).is_active
    assert not Team.objects.get(pk=linked_team.pk).channels_need_clearing
    assert not DiscordTask.objects.exists()


def test_a_different_school_unlinks_the_members_and_clears_the_channels(client, linked_team):
    from core.models import AuditLog, DiscordTask
    from team.models import DiscordLink

    with patch("core.views.reset_team_credentials", return_value=_reset()) as reset:
        response = _edit(client, 4, "Other College", change="new_school")

    reset.assert_called_once_with(4, actor="goldteam")
    shown = [str(m) for m in get_messages(response.wsgi_request)]
    assert shown == [
        "Team 4 now holds Other College: 1 Discord member(s) unlinked; new password stored for the packet email; "
        "ended the team's Authentik sessions."
    ]
    assert SchoolInfo.objects.get(team=linked_team).school_name == "Other College"
    assert not DiscordLink.objects.get(discord_id=44).is_active
    audit = AuditLog.objects.get(action="user_unlinked")
    assert (audit.target_id, audit.details["reason"]) == (44, "school_changed")
    assert Team.objects.get(pk=linked_team.pk).channels_need_clearing
    queued = sorted(DiscordTask.objects.values_list("task_type", flat=True))
    assert queued == ["clear_team_channels", "sync_member_roles"]


def test_a_different_school_says_when_the_password_reset_failed(client, linked_team):
    from core.views import RESET_TEAM_INSTEAD

    with patch("core.views.reset_team_credentials", return_value=_reset(password=None, error="Authentik down")):
        response = _edit(client, 4, "Other College", change="new_school")

    shown = [str(m) for m in get_messages(response.wsgi_request)]
    assert shown == [
        "Team 4 now holds Other College: 1 Discord member(s) unlinked, but the password reset failed: Authentik down. "
        + RESET_TEAM_INSTEAD
    ]


def test_unlinking_one_team_locks_its_row_first(linked_team):
    """As a link does: a link that took the team lock first is unlinked; one landing after the edit's lock stays
    (until the link flow re-checks the team)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from registration.services import unlink_teams

    with CaptureQueriesContext(connection) as queries:
        unlink_teams("gold", "school_changed", linked_team)

    first = queries.captured_queries[0]["sql"]
    assert '"team_team"' in first and "FOR UPDATE" in first


def test_a_rename_keeps_the_school_row_and_its_registration(client, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    school = School.for_name("Example University")
    EventTeamAssignment.objects.create(event=event, team=team, school=school, registration=registration)
    SchoolInfo.objects.create(team=team, school=school, contact_email="coach@example.edu")

    _edit(client, 4, "Example State University", change="rename")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert (assignment.school_id, assignment.registration_id) == (school.pk, registration.pk)
    assert School.objects.get(pk=school.pk).name == "Example State University"
    assert not School.objects.exclude(pk=school.pk).exists()


def test_a_rename_to_a_school_on_record_moves_the_team_onto_it(client, event):
    """The spelling another record already has: the team takes that record, keeping its registration, members and
    channels, and neither record is renamed."""
    team = Team.objects.create(team_number=4, team_name="Team 04")
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    school = School.for_name("Example University")
    EventTeamAssignment.objects.create(event=event, team=team, school=school, registration=registration)
    SchoolInfo.objects.create(team=team, school=school, contact_email="coach@example.edu")
    other = School.for_name("Example Univ.")

    _edit(client, 4, "example univ.", change="rename")

    assignment = EventTeamAssignment.objects.get(event=event)
    assert (assignment.school_id, assignment.registration_id) == (other.pk, registration.pk)
    assert SchoolInfo.objects.get(team=team).school_id == other.pk
    names = dict(School.objects.values_list("pk", "name"))
    assert names == {school.pk: "Example University", other.pk: "Example Univ."}
    assert not Team.objects.get(pk=team.pk).channels_need_clearing


def test_a_rename_to_a_school_another_team_holds_is_refused(client, event, linked_team):
    other_team = Team.objects.create(team_number=2, team_name="Team 02")
    EventTeamAssignment.objects.create(event=event, team=other_team, school=School.for_name("Other College"))

    response = _edit(client, 4, "other college", change="rename")

    assert "Other College already has a team: Team 2." in response.content.decode()
    assert SchoolInfo.objects.get(team=linked_team).school.name == "Example University"


def test_a_rename_carries_the_open_events_registrations_under_the_old_name(client, event, linked_team):
    """Or Assign Teams would read the old name as a school with no team, and give it a second one."""
    from registration.models import RegistrationEventEnrollment

    duplicate = TeamRegistration.objects.create(school_name="example  university", status="approved")
    RegistrationEventEnrollment.objects.create(registration=duplicate, event=event)

    _edit(client, 4, "Example State University", change="rename")

    duplicate.refresh_from_db()
    assert duplicate.school_name == "Example State University"


def test_an_edit_from_a_stale_page_is_refused(client, linked_team):
    """Another edit gave the team another school meanwhile: renaming would rename that school's record instead."""
    info = SchoolInfo.objects.get(team=linked_team)
    shown = (info.school_id, info.school.name_key)
    SchoolInfo.objects.filter(team=linked_team).update(school=School.for_name("Other College"))

    response = _edit(client, 4, "Example State University", change="rename", shown=shown)

    assert "school changed since you opened this page" in response.content.decode()
    assert dict(School.objects.values_list("name_key", "name"))["other college"] == "Other College"


def test_a_first_school_for_a_team_with_linked_members_unlinks_and_resets_them(client):
    """No school on the list, but members linked from an earlier one: the new school starts clean too."""
    from django.contrib.auth.models import User

    from team.models import DiscordLink

    team = Team.objects.create(team_number=4, team_name="Team 04")
    user = User.objects.create(username="member04")
    DiscordLink.objects.create(discord_id=44, discord_username="m", user=user, team=team, is_active=True)

    with patch("core.views.reset_team_credentials", return_value=_reset()) as reset:
        _edit(client, 4, "Other College")

    reset.assert_called_once_with(4, actor="goldteam")
    assert not DiscordLink.objects.get(discord_id=44).is_active
    assert SchoolInfo.objects.get(team=team).school_name == "Other College"


def test_a_different_school_whose_sessions_stay_is_a_warning(client, linked_team):
    from django.contrib.messages import constants

    reset = _reset()
    reset = type(reset)(password="new-pass", sessions_error="Authentik down")
    with patch("core.views.reset_team_credentials", return_value=reset):
        response = _edit(client, 4, "Other College", change="new_school")

    (message,) = list(get_messages(response.wsgi_request))
    assert message.level == constants.WARNING
    assert "could not end the team's Authentik sessions: Authentik down" in str(message)


@pytest.mark.django_db(transaction=True)
def test_a_different_school_and_an_import_at_once_both_finish(client, event):
    """The edit takes the school list lock before the team row's, as the import does, so the two can't deadlock."""
    import threading
    import time

    from django.db import connection
    from registration.services import SchoolListRow, import_school_list, set_team_school

    import core.views

    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="coach@example.edu")
    real_unlink = core.views.unlink_teams
    unlinked = threading.Event()
    errors: list[Exception] = []

    def unlink_then_wait(*args, **kwargs):
        count = real_unlink(*args, **kwargs)
        unlinked.set()
        time.sleep(2)
        return count

    def edit():
        try:
            _edit(client, 4, "Other College", change="new_school")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connection.close()

    def import_list():
        unlinked.wait(10)
        try:
            row = SchoolListRow(team_number=4, school_name="Third College", contact_email="c@example.edu")
            import_school_list(event, [row], "gold")
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            connection.close()

    with (
        patch("core.views.unlink_teams", unlink_then_wait),
        patch("core.views.reset_team_credentials", return_value=_reset()),
    ):
        threads = [threading.Thread(target=edit), threading.Thread(target=import_list)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

    assert errors == []
    assert SchoolInfo.objects.get(team=team).school_name == "Third College"


def test_an_edit_from_a_page_shown_before_a_rename_is_refused(client, linked_team):
    """Same school row, renamed since: saving would undo the rename, or read the page as naming a different school."""
    info = SchoolInfo.objects.get(team=linked_team)
    shown = (info.school_id, info.school.name_key)
    School.objects.filter(pk=info.school_id).update(
        name="Example State University", name_key="example state university"
    )

    response = _edit(client, 4, "Example University", change="rename", shown=shown)

    assert "school changed since you opened this page" in response.content.decode()
    assert School.objects.get(pk=info.school_id).name == "Example State University"


def test_the_rename_question_reads_the_school_under_the_lock(client, linked_team):
    """The school read before the lock is the one the page showed; the question follows the one held now."""
    from registration import locks

    from core.views import SAY_RENAME_OR_NEW

    real_lock = locks.school_list_lock

    def rename_then_lock():
        School.objects.filter(name_key="example university").update(name="Other College", name_key="other college")
        real_lock()

    with patch("core.views.school_list_lock", rename_then_lock):
        response = _edit(client, 4, "Other College", change="")

    assert SAY_RENAME_OR_NEW not in response.content.decode()
    assert "school changed since you opened this page" in response.content.decode()
