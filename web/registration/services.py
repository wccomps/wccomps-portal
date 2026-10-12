from dataclasses import dataclass

from django.db import transaction
from packets.models import Packet

from core.competition_utils import live_competition_data_exists
from team.models import SchoolInfo, Team, default_team_name

from .locks import school_list_lock
from .models import Event, EventTeamAssignment, School, TeamRegistration


class PacketsSentError(Exception):
    """Packets went out for the open event, so its teams' schools can only be changed, not removed."""


class SchoolTakenError(Exception):
    """Another team already holds the school; the message names the school and that team."""


class TeamTakenError(Exception):
    """The team was expected to be free but holds another school; the message names the team and the school."""


class SchoolChangedError(Exception):
    """The team's school is no longer the one the page showed."""


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
    expect_free: bool = False,
) -> SchoolInfo:
    """Make the school the one holding this team in the school list, and at the open event if there is one.

    The only writer of a team's school: the open event's assignment follows the school list, linked to the
    registration given or, while the school stays the same, the one already linked. Raises SchoolTakenError when
    another team holds the school, and with expect_free, TeamTakenError when this team holds another school.
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

    fields = {
        "school": school,
        "contact_email": contact_email,
        "secondary_email": secondary_email,
        "notes": notes,
        "updated_by": updated_by,
    }
    info = SchoolInfo.objects.filter(team=team).select_related("school").first()
    if expect_free and info is not None and info.school_id != school.pk:
        raise TeamTakenError(f"Team {team.team_number} already has a school: {info.school_name}.")
    if info is None:
        info = SchoolInfo.objects.create(team=team, **fields)
    else:
        # Never the password: a packet send or password reset may have set it since this row was read
        for name, value in fields.items():
            setattr(info, name, value)
        info.save(update_fields=[*fields, "updated_at"])

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


def packets_sent(event: Event | None) -> bool:
    """Whether any packet went out for the event; sent packets reached its teams under their schools."""
    return event is not None and Packet.objects.sent().filter(event=event).exists()


def activate_teams(teams: list[Team]) -> int:
    """Activate the teams given a school: only active teams show on School Info, get packets and count in standings.

    Returns how many were inactive."""
    return Team.objects.filter(pk__in=[team.pk for team in teams], is_active=False).update(is_active=True)


@transaction.atomic
def remove_team_school(team: Team, expected_school_id: int | None = None) -> None:
    """Take the team out of the school list and out of the open event; earlier events keep their assignments.

    PacketsSentError when packets went out for the open event: they reached the team under this school.
    SchoolChangedError when expected_school_id (the school the page showed) is no longer the team's school.
    """
    school_list_lock()
    event = Event.objects.open()
    if expected_school_id is not None:
        current = SchoolInfo.objects.filter(team=team).values_list("school_id", flat=True).first()
        if current is None and event is not None:
            current = event.team_assignments.filter(team=team).values_list("school_id", flat=True).first()
        if current != expected_school_id:
            raise SchoolChangedError(f"Team {team.team_number}'s school changed since you opened this page; reload.")
    if packets_sent(event):
        raise PacketsSentError(
            f"Packets have already been sent for {event}. Change the team's school on its School Info page instead."
        )
    SchoolInfo.objects.filter(team=team).delete()
    if event is not None:
        event.team_assignments.filter(team=team).delete()


@transaction.atomic
def clear_school_list() -> int:
    """Empty the school list and the open event's assignments with it; earlier events keep theirs.

    Returns how many school info records were deleted; PacketsSentError when packets went out for the open event.
    """
    school_list_lock()
    event = Event.objects.open()
    if packets_sent(event):
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
    if packets_sent(open_now):
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


@dataclass(frozen=True)
class SchoolListRow:
    """One line of an imported school list: the school that holds the team, and its contacts."""

    team_number: int
    school_name: str
    contact_email: str
    secondary_email: str = ""
    notes: str = ""


@transaction.atomic
def import_school_list(event: Event, rows: list[SchoolListRow], updated_by: str) -> dict[str, int]:
    """Replace the school list with the rows and open the event (saving it first when new), replacing its
    assignments: each row's school holds the row's team. Resets every team name and activates the rows' teams.

    Under the school list lock; the event is re-checked under it, and ImportBlockedError says why the import can't
    go ahead.
    """
    school_list_lock()
    if problem := import_problem(event):
        raise ImportBlockedError(problem)
    teams = Team.objects.in_bulk([row.team_number for row in rows], field_name="team_number")
    if missing := [str(row.team_number) for row in rows if row.team_number not in teams]:
        raise ImportBlockedError(f"These teams no longer exist: {', '.join(missing)}. Upload the file again.")
    if event._state.adding:
        event.save()

    # Opened before the clear, so the clear also replaces assignments the event had while it wasn't open
    event.open()
    clear_school_list()
    # Names like a school's shortname belong to the last event's assignment, not the team slot
    all_teams = list(Team.objects.all())
    for team in all_teams:
        team.team_name = default_team_name(team.team_number)
    Team.objects.bulk_update(all_teams, ["team_name"])
    activated = activate_teams(list(teams.values()))

    for row in rows:
        set_team_school(
            teams[row.team_number],
            row.school_name,
            contact_email=row.contact_email,
            secondary_email=row.secondary_email,
            notes=row.notes,
            updated_by=updated_by,
        )
    return {"created": len(rows), "activated": activated}
