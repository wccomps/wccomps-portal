from typing import TYPE_CHECKING

from django.db import connection, transaction
from packets.models import Packet

from core.competition_utils import live_competition_data_exists
from team.models import SchoolInfo, Team, default_team_name

from .models import Event, EventTeamAssignment, School, TeamRegistration

if TYPE_CHECKING:
    from team.forms import CSVRowData


# Arbitrary, but fixed (and not prepare_database's): every school-list writer asks for the same lock.
SCHOOL_LIST_LOCK_ID = 7_261_640_002


def school_list_lock() -> None:
    """Wait for the school list, then hold it until the transaction ends.

    Every write to the school list or the open event's assignments, and every packet send, takes it first and reads
    the open event after it. Re-entrant within one transaction, so an import can call set_team_school.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])


class PacketsSentError(Exception):
    """Packets went out for the open event, so its teams' schools can only be changed, not removed."""


class SchoolTakenError(Exception):
    """Another team already holds the school; the message names the school and that team."""


@transaction.atomic
def set_team_school(
    team: Team,
    school_name: str,
    *,
    contact_email: str,
    secondary_email: str = "",
    notes: str = "",
    updated_by: str = "",
    registration: TeamRegistration | None = None,
) -> SchoolInfo:
    """Make the school the one holding this team in the school list, and at the open event if there is one.

    The only writer of a team's school: the open event's assignment follows the school list, linked to the
    registration given or, while the school stays the same, the one already linked. Raises SchoolTakenError when
    another team holds the school.
    """
    school_list_lock()
    school = School.for_name(school_name)
    event = Event.objects.open()
    holder: SchoolInfo | EventTeamAssignment | None = (
        SchoolInfo.objects.filter(school=school).exclude(team=team).select_related("team").first()
    )
    if holder is None and event is not None:
        holder = event.team_assignments.filter(school=school).exclude(team=team).select_related("team").first()
    if holder is not None:
        raise SchoolTakenError(f"{school.name} already has a team: Team {holder.team.team_number}.")

    info = SchoolInfo.objects.filter(team=team).first() or SchoolInfo(team=team)
    info.school = school
    info.contact_email = contact_email
    info.secondary_email = secondary_email
    info.notes = notes
    info.updated_by = updated_by
    info.save()

    if event is not None:
        assignment = event.team_assignments.filter(team=team).select_related("registration").first()
        if assignment is None:
            assignment = EventTeamAssignment.objects.create(
                event=event, team=team, school=school, registration=registration
            )
        elif assignment.school_id != school.pk or (registration and assignment.registration_id != registration.pk):
            # Another school's registration doesn't follow the team to its new school
            assignment.school = school
            assignment.registration = registration
            assignment.save(update_fields=["school", "registration"])
        registration = assignment.registration
    if registration is not None and registration.school_id != school.pk:
        registration.school = school
        registration.save(update_fields=["school"])
    return info


@transaction.atomic
def remove_team_school(team: Team) -> None:
    """Take the team out of the school list and out of the open event; earlier events keep their assignments."""
    school_list_lock()
    SchoolInfo.objects.filter(team=team).delete()
    event = Event.objects.open()
    if event is not None:
        event.team_assignments.filter(team=team).delete()


@transaction.atomic
def clear_school_list() -> int:
    """Empty the school list and the open event's assignments with it; earlier events keep theirs.

    Returns how many school info records were deleted; PacketsSentError when packets went out for the open event.
    """
    school_list_lock()
    event = Event.objects.open()
    if event is not None and Packet.objects.sent().filter(event=event).exists():
        raise PacketsSentError(
            f"Packets have already been sent for {event}. Clear the event's results instead (Finalize and clear), "
            "or change single schools on their School Info pages."
        )
    deleted, _ = SchoolInfo.objects.all().delete()
    if event is not None:
        event.team_assignments.all().delete()
    return deleted


class ImportBlockedError(Exception):
    """Why a school list can't be imported for the event now."""


def import_problem(event: Event) -> str:
    """Why this event can't be imported into now, or ''. An unsaved event is a new one."""
    open_now = Event.objects.open()
    if open_now and open_now != event:
        return f"{open_now} is still open. Finalize and clear it before importing a list for another event."
    if open_now and Packet.objects.sent().filter(event=open_now).exists():
        return (
            f"Packets have already been sent for {open_now}. Re-importing reassigns team numbers, so sent credentials "
            "would reach the wrong school. Change a single school on its School Info page instead."
        )
    if not live_competition_data_exists():
        return ""
    if open_now is None:
        return (
            "Scores from the last event are still in the portal. "
            "Finalize and clear that event before importing a new school list."
        )
    return (
        f"Teams have already started using their team numbers for {open_now} (scores, tickets or Discord links). "
        "Re-importing reassigns team numbers under them. Change a single school on its School Info page instead."
    )


@transaction.atomic
def import_school_list(event: Event, rows: list[CSVRowData], updated_by: str) -> dict[str, int]:
    """Replace the school list with the rows and open the event (saving it first when new), replacing its
    assignments: each row's school holds the row's team. Resets every team name and activates the rows' teams.

    Every school-list write and packet send takes the same lock; the event is re-checked under it, and
    ImportBlockedError says why the import can't go ahead.
    """
    school_list_lock()
    if problem := import_problem(event):
        raise ImportBlockedError(problem)
    if event._state.adding:
        event.save()

    SchoolInfo.objects.all().delete()
    # Names like a school's shortname belong to the last event's assignment, not the team slot
    teams = list(Team.objects.all())
    for team in teams:
        team.team_name = default_team_name(team.team_number)
    Team.objects.bulk_update(teams, ["team_name"])
    EventTeamAssignment.objects.filter(event=event).delete()
    activated = Team.objects.filter(pk__in=[row["_team"].pk for row in rows], is_active=False).update(is_active=True)
    event.open()

    for row in rows:
        set_team_school(
            row["_team"],
            row["school_name"],
            contact_email=row["contact_email"],
            secondary_email=row.get("secondary_email", ""),
            notes=row.get("notes", ""),
            updated_by=updated_by,
        )
    return {"created": len(rows), "activated": activated}
