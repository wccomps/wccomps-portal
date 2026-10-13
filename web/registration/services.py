import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import cast

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, QuerySet
from packets.models import Packet
from scoring.calculator import compute_standings
from scoring.export import export_all_zip_bytes
from scoring.scorecard import build_scorecard_context
from scoring.snapshot import SNAPSHOT_VERSION, context_from_snapshot, serialize_scorecard

from core.competition_utils import live_competition_data_exists, wipe_competition_data
from core.models import AuditLog
from team.models import DiscordLink, SchoolInfo, Team, default_team_name
from ticketing.models import Ticket

from .locks import school_list_lock
from .models import (
    Event,
    EventArchive,
    EventResult,
    EventTeamAssignment,
    School,
    Season,
    TeamRegistration,
    normalize_school_name,
)


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
    """Why a school list can't be imported into or added to the event now."""


def import_problem(event: Event) -> str:
    """Why this event can't be imported into now, or ''. An unsaved event is a new one."""
    open_now = Event.objects.open()
    if open_now and open_now != event:
        return f"{open_now} is still open. Finalize and clear it before importing a list for another event."
    if packets_sent(open_now):
        return (
            f"Packets have already been sent for {open_now}. Re-importing reassigns team numbers, so sent credentials "
            "would reach the wrong school. Change a single school on its School Info page instead. "
            "To add late schools, use Add schools."
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
        "Re-importing reassigns team numbers under them. Change a single school on its School Info page instead. "
        "To add late schools, use Add schools."
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


ADD_NEEDS_OPEN_EVENT = "Add schools needs the open event. Use Replace for an event's first school list."


def add_problem(event: Event) -> str:
    """Why schools can't be added to this event now, or ''. Only the open event's list can grow."""
    return "" if Event.objects.open() == event else ADD_NEEDS_OPEN_EVENT


def free_teams(event: Event) -> QuerySet[Team]:
    """Teams no school holds and nobody used: not on the school list, not assigned at the event (the open one), and
    carrying no per-team data a school left behind (blue team Discord links, tickets, a ticket counter)."""
    return Team.objects.filter(
        ~Exists(SchoolInfo.objects.filter(team=OuterRef("pk"))),
        ~Exists(event.team_assignments.filter(team=OuterRef("pk"))),
        ~Exists(DiscordLink.objects.filter(team=OuterRef("pk"))),
        ~Exists(Ticket.objects.filter(team=OuterRef("pk"))),
        ticket_counter=0,
    )


def held_schools(name_keys: Iterable[str], event: Event) -> set[str]:
    """The name keys of these schools that already hold a team: on the school list, or at the event."""
    keys = list(name_keys)
    on_list = SchoolInfo.objects.filter(school__name_key__in=keys).values_list("school__name_key", flat=True)
    at_event = event.team_assignments.filter(school__name_key__in=keys).values_list("school__name_key", flat=True)
    return set(on_list) | set(at_event)


@transaction.atomic
def add_to_school_list(event: Event, rows: list[SchoolListRow], updated_by: str) -> dict[str, int]:
    """Add the rows' schools to the open event's school list, each holding the row's team, which must be free.

    Moves nobody: existing schools keep their teams, assignments and team names, so it is allowed after packets
    went out or teams started using their numbers. Resets the new teams' names and activates them. Under the
    school list lock; ImportBlockedError, writing nothing, when the event is no longer open, a team was given a
    school or a school was put on the list since the preview.
    """
    school_list_lock()
    if problem := add_problem(event):
        raise ImportBlockedError(problem)
    teams = Team.objects.in_bulk([row.team_number for row in rows], field_name="team_number")
    if missing := [str(row.team_number) for row in rows if row.team_number not in teams]:
        raise ImportBlockedError(f"These teams no longer exist: {', '.join(missing)}. Upload the file again.")
    free = set(free_teams(event).filter(pk__in=[team.pk for team in teams.values()]).values_list("pk", flat=True))
    if taken := [row.team_number for row in rows if teams[row.team_number].pk not in free]:
        raise ImportBlockedError(f"Team {taken[0]} was given a school since the preview. Upload the file again.")
    held = held_schools((normalize_school_name(row.school_name) for row in rows), event)
    if listed := [row.school_name for row in rows if normalize_school_name(row.school_name) in held]:
        raise ImportBlockedError(f"{listed[0]} is already on the list. Upload the file again.")

    new_teams = list(teams.values())
    for team in new_teams:
        team.team_name = default_team_name(team.team_number)
    Team.objects.bulk_update(new_teams, ["team_name"])
    activated = activate_teams(new_teams)
    for row in rows:
        try:
            set_team_school(
                teams[row.team_number],
                row.school_name,
                contact_email=row.contact_email,
                secondary_email=row.secondary_email,
                notes=row.notes,
                updated_by=updated_by,
            )
        except SchoolTakenError:
            # Only when two rows name one school, which the preview refuses
            raise ImportBlockedError(f"{row.school_name} is already on the list. Upload the file again.") from None
    return {"created": len(rows), "activated": activated}


class FinalizeError(Exception):
    """Finalize, Clear or opening an event for the current schools refused; the message says why."""


@transaction.atomic
def open_event_for_current_schools(name: str, event_date: date, event_type: str) -> Event:
    """Create the event in the active season and open it for the school list as it stands, for an event that runs
    without an import: each school on the list is assigned its team at the event, and the packets sent while no
    event was open are stamped with it. Under the school list lock; FinalizeError while an event is open or with no
    active season.
    """
    school_list_lock()
    if open_now := Event.objects.open():
        raise FinalizeError(f"{open_now} is already open. Finalize and clear it first.")
    season = Season.objects.active()
    if season is None:
        raise FinalizeError("There is no active season to hold the event. Activate a season first.")
    # Schools come from the list, so the public registration form doesn't offer it
    if Event.objects.filter(season=season, name=name).exists():
        raise FinalizeError(f"The {season} season already has an event named {name}. Choose another name.")
    try:
        with transaction.atomic():
            event = Event.objects.create(
                season=season, name=name, date=event_date, event_type=event_type, registration_open=False
            )
    except IntegrityError:
        raise FinalizeError(f"The {season} season cannot hold another {event_type} event numbered the same.") from None
    event.open()
    _assign_school_list(event)
    Packet.objects.sent().filter(event=None).update(event=event)
    return event


def _assign_school_list(event: Event) -> None:
    """Record each school on the list as holding its team at the new event; the list itself stays as it is."""
    EventTeamAssignment.objects.bulk_create(
        EventTeamAssignment(event=event, school_id=school_id, team_id=team_id)
        for school_id, team_id in SchoolInfo.objects.values_list("school_id", "team_id")
    )


def current_snapshots(event: Event) -> dict[int, dict[str, object]]:
    """Each school assigned at the event, by id, with its scorecard as Finalize would freeze it now."""
    school_by_team = dict(event.team_assignments.values_list("team_id", "school_id"))
    standings = compute_standings()
    return {
        school_by_team[standing.team.pk]: serialize_scorecard(build_scorecard_context(standing, standings))
        for standing in standings
        if standing.team.pk in school_by_team
    }


def _lock_open_event(event: Event) -> Event:
    """The event re-read with its row locked, once it is confirmed to be the open one. Call under the school list
    lock; FinalizeError when the event isn't open, since the live scores belong only to the open event."""
    if Event.objects.open() != event:
        raise FinalizeError(f"{event} is not open; only the open event's live scores can be finalized or cleared.")
    return Event.objects.select_for_update().get(pk=event.pk)


@transaction.atomic
def finalize_event(event: Event, actor: str) -> int:
    """Freeze each assigned school's scorecard and the export zip for the open event; finalizing again replaces both.

    Returns how many results were written. Under the school list lock, so the schools can't change underneath.
    """
    school_list_lock()
    event = _lock_open_event(event)
    snapshots = current_snapshots(event)
    team_numbers = dict(event.team_assignments.values_list("school_id", "team__team_number"))
    EventResult.objects.filter(event=event).delete()
    EventResult.objects.bulk_create(
        _result(event, school_id, team_numbers[school_id], scorecard) for school_id, scorecard in snapshots.items()
    )
    EventArchive.objects.update_or_create(event=event, defaults={"data": export_all_zip_bytes()})
    event.is_finalized = True
    event.save(update_fields=["is_finalized", "updated_at"])
    AuditLog.objects.create(
        action="event_finalized",
        admin_user=actor,
        target_entity="event",
        target_id=event.pk,
        details={"event": event.name, "results": len(snapshots)},
    )
    return len(snapshots)


def _result(event: Event, school_id: int, team_number: int, scorecard: dict[str, object]) -> EventResult:
    score = cast(dict[str, object], context_from_snapshot(scorecard)["score"])
    return EventResult(
        event=event,
        school_id=school_id,
        team_number=team_number,
        rank=cast(int | None, score["rank"]),
        total_score=cast(Decimal, score["total_score"]),
        scorecard=scorecard,
        version=SNAPSHOT_VERSION,
    )


def snapshot_matches_live(event: Event) -> bool:
    """Whether the stored results say exactly what the live scores say now."""
    stored = {result.school_id: result.scorecard for result in EventResult.objects.filter(event=event)}
    # Each scorecard through JSON, as the stored ones went (tuples become lists); the school ids stay ints
    live = {school_id: json.loads(json.dumps(scorecard)) for school_id, scorecard in current_snapshots(event).items()}
    return stored == live


@transaction.atomic
def clear_event(event: Event, actor: str) -> dict[str, int]:
    """Delete the live competition data once the open event's results are frozen and still match it, then close
    the event. The school list stays until the next import replaces it.

    Returns the deleted counts (wipe_competition_data). Under the school list lock; FinalizeError when the event
    isn't open, isn't finalized, or its scores changed since finalizing.
    """
    school_list_lock()
    event = _lock_open_event(event)
    if not event.is_finalized:
        raise FinalizeError("Finalize this event first.")
    if not snapshot_matches_live(event):
        raise FinalizeError("Results changed since finalizing; finalize again before clearing.")
    # Late changes the scorecard doesn't show (a ticket comment, an incident note) go into the archive
    EventArchive.objects.update_or_create(event=event, defaults={"data": export_all_zip_bytes()})
    counts = wipe_competition_data()
    event.close()
    deleted = {k: v for k, v in counts.items() if k != "TeamTicketCounter"}
    AuditLog.objects.create(
        action="event_cleared",
        admin_user=actor,
        target_entity="event",
        target_id=event.pk,
        details={
            "event": event.name,
            "deleted_counts": {k: v for k, v in deleted.items() if v},
            "total_deleted": sum(deleted.values()),
            "ticket_counters_reset": counts["TeamTicketCounter"],
        },
    )
    return counts


class EventKeptError(Exception):
    """The event, or an event in the season, is part of the season's history; the message says why."""


def event_delete_problem(event: Event) -> str:
    """Why the event can't be deleted, or ''. Open, finalized and assigned events are kept as season history."""
    if event.is_active:
        return f"{event} is open: the live scores and school list belong to it."
    if event.is_finalized or EventResult.objects.filter(event=event).exists():
        return f"{event} has finalized results, which are kept."
    if event.team_assignments.exists():
        return f"{event} has schools assigned to teams, which are kept as the season's history."
    return ""


def season_delete_problem(season: Season) -> str:
    """Why the season can't be deleted (the first event it must keep), or ''."""
    for event in season.events.all():
        if problem := event_delete_problem(event):
            return f"The {season} season holds an event that is kept: {problem}"
    return ""


@transaction.atomic
def delete_event(event: Event) -> None:
    """Delete an event no history depends on. Under the school list lock, so it can't be opened or assigned
    schools meanwhile; EventKeptError says why it is kept."""
    school_list_lock()
    event = Event.objects.select_for_update().get(pk=event.pk)
    if problem := event_delete_problem(event):
        raise EventKeptError(problem)
    event.delete()


@transaction.atomic
def delete_season(season: Season) -> None:
    """Delete a season and its events when none of them is kept. Under the school list lock, like delete_event."""
    school_list_lock()
    season = Season.objects.select_for_update().get(pk=season.pk)
    if problem := season_delete_problem(season):
        raise EventKeptError(problem)
    season.delete()
