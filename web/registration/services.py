from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from django.db import OperationalError, connection, transaction
from django.db.models import Exists, OuterRef, QuerySet
from packets.models import Packet, packets_sent
from scoring.calculator import Standing, compute_standings
from scoring.export import export_all_zip_bytes
from scoring.models import QuotientMetadataCache
from scoring.scorecard import build_scorecard_context
from scoring.snapshot import SNAPSHOT_VERSION, serialize_scorecard

from core.competition_utils import WipedData, wipe_competition_data
from core.discord_tasks import CleanupCompetition
from core.models import AuditLog, CompetitionConfig, DiscordTask, QueuedAnnouncement
from team.models import DiscordLink, SchoolInfo, Team, default_team_name
from ticketing.models import Ticket

from .locks import school_list_held, school_list_lock
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
from .stage import Action, EventStage, Stage, event_stage, scored_teams


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
            f"Packets have already been sent for {event}. Close it out instead (Close Out Event on the Competition "
            "page), or change single schools on their School Info pages."
        )
    deleted, _ = SchoolInfo.objects.all().delete()
    if event is not None:
        event.team_assignments.all().delete()
    return deleted


class ImportBlockedError(Exception):
    """Why a school list can't be imported into or added to the event now."""


@dataclass(frozen=True)
class NewSeason:
    """A season to create, as the only active one, with the event it holds."""

    name: str
    year: int

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class NewEvent:
    """An event to create for a school list: the import's new event, or the event Close Out records for scores
    that ran without an import. seen_season: the id of the season that was active when the page showed it (None when
    none was); the event is refused if another is active now. season: a new season for it, from the import only:
    needed when none is active, or started at a new school year."""

    name: str
    date: date
    event_type: str
    seen_season: int | None
    season: NewSeason | None = None

    def __str__(self) -> str:
        return f"New: {self.name}" + (f", in new season {self.season}" if self.season else "")


def new_event_problem(new_event: NewEvent) -> str:
    """Why the new event (and its new season) can't be created now, or ''."""
    active = Season.objects.active()
    if (active.pk if active else None) != new_event.seen_season:
        return "The active season changed since this page loaded. Reload the page."
    if new_event.season is None:
        if active is None:
            return "No season is active: give the new season a name and a year."
        if Event.objects.filter(season=active, name=new_event.name).exists():
            return f"The {active} season already has an event named {new_event.name}. Choose another name."
        return ""
    if taken := Season.objects.filter(year=new_event.season.year).first():
        if taken.is_active:
            return (
                f"{taken.year} is the year of the active season, {taken}: leave the new season empty to put the "
                "event in it."
            )
        return (
            f"The {taken} season already has the year {new_event.season.year}. Pick another year, or activate "
            "that season."
        )
    return ""


def _create_event(new_event: NewEvent) -> Event:
    """Save the new event, not yet open, in the active season or in its new season, which becomes the only active
    one. Call under the school list lock once new_event_problem has nothing to say."""
    if new_event.season is None:
        season = Season.objects.get(is_active=True)
    else:
        season = Season.objects.create(name=new_event.season.name, year=new_event.season.year)
        season.activate()
    return Event.objects.create(
        season=season,
        name=new_event.name,
        date=new_event.date,
        event_type=new_event.event_type,
        # Schools come from the list, not the public registration form
        registration_open=False,
    )


def import_problem(event: Event | NewEvent) -> str:
    """Why this event can't be imported into now, or ''."""
    stage = event_stage()
    open_now = stage.event
    if open_now and open_now != event:
        instead = Action.CANCEL if stage.stage == Stage.READY else Action.CLOSE_OUT
        return f"{open_now} is still open. {stage.instruction(instead)} Then import the list for another event."
    if packets_sent(open_now):
        return (
            f"Packets have already been sent for {open_now}. Re-importing reassigns team numbers, so sent credentials "
            "would reach the wrong school. Change a single school on its School Info page instead. "
            "To add late schools, use Add schools."
        )
    if not stage.live_data:
        return ""
    if open_now is None:
        return f"{stage.status} {stage.steps_left}"
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
def import_school_list(event: Event | NewEvent, rows: list[SchoolListRow], updated_by: str) -> dict[str, int]:
    """Replace the school list with the rows and open the event (creating it first when new, and its new season as
    the only active one), replacing its assignments: each row's school holds the row's team. Resets every team name
    and activates the rows' teams.

    Under the school list lock; the event is re-checked under it, and ImportBlockedError says why the import can't
    go ahead.
    """
    school_list_lock()
    if problem := import_problem(event):
        raise ImportBlockedError(problem)
    teams = Team.objects.in_bulk([row.team_number for row in rows], field_name="team_number")
    if missing := [str(row.team_number) for row in rows if row.team_number not in teams]:
        raise ImportBlockedError(f"These teams no longer exist: {', '.join(missing)}. Upload the file again.")
    if isinstance(event, NewEvent):
        if problem := new_event_problem(event):
            raise ImportBlockedError(problem)
        event = _create_event(event)

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


def add_problem(event: Event | NewEvent) -> str:
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
def add_to_school_list(event: Event | NewEvent, rows: list[SchoolListRow], updated_by: str) -> dict[str, int]:
    """Add the rows' schools to the open event's school list, each holding the row's team, which must be free.

    Moves nobody: existing schools keep their teams, assignments and team names, so it is allowed after packets
    went out or teams started using their numbers. Resets the new teams' names and activates them. Under the
    school list lock; ImportBlockedError, writing nothing, when the event is no longer open, a team was given a
    school or a school was put on the list since the preview.
    """
    school_list_lock()
    if (problem := add_problem(event)) or not isinstance(event, Event):
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


class CloseOutError(Exception):
    """Close Out Event or Clear leftover data refused; the message says why."""


def _lock_config() -> CompetitionConfig:
    """The competition config, its row locked until commit; the stage read after it sees whether the competition
    runs. A start writes applications_enabled under the same row lock, so it can't mark the competition running
    until this commits. A start already under way may have turned Authentik on before it waits, and the queued
    Discord cleanup checks for a running competition only when the bot picks it up, so it can still run as such a
    start lands."""
    return CompetitionConfig.objects.select_for_update().get(pk=CompetitionConfig.get_config().pk)


def _running_refusal(stage: EventStage) -> str:
    return f"The competition is running. {stage.next_step}"


def close_out_problem(stage: EventStage, *, check_schools: bool = True) -> str:
    """Why the stage can't be closed out now, or ''. With check_schools, also whether every score would be kept:
    each listed school's team active, and every scored team holding a school. The page asks without it first."""
    if stage.running:
        return _running_refusal(stage)
    if stage.stage == Stage.READY:
        return (
            f"No team has scores yet, so there is nothing to close out for {stage.event}. "
            f"{stage.instruction(Action.CANCEL)}"
        )
    if not stage.played:
        return f"Nothing to close out. {stage.status} {stage.next_step}"
    if stage.event is None and not Season.objects.filter(is_active=True).exists():
        return (
            "No season is active, and Close Out records these scores at a new event in the active season. Make a "
            "season active on the Seasons page, then close out."
        )
    return _unfreezable(stage.event)[0] if check_schools else ""


def close_out_fixes(stage: EventStage) -> list[TeamFix]:
    """The teams close_out_problem names as keeping Close Out from keeping their scores, and how each is fixed."""
    return _unfreezable(stage.event)[1] if stage.played else []


def _refuse_a_stale_target(target: Event | NewEvent, stage: EventStage) -> None:
    """CloseOutError when the open event is no longer what the page showed."""
    if isinstance(target, NewEvent):
        if stage.event is not None:
            raise CloseOutError(f"{stage.event} was opened since the page loaded. Reload the page.")
    elif stage.event != target:
        raise CloseOutError(f"{target} is no longer open. Reload the page.")


def _locked_target(target: Event | NewEvent, stage: EventStage) -> Event:
    """The open event, its row locked; or the new event target, created and opened for the current school list.
    Call under the school list lock once the target and the stage passed their checks."""
    if not isinstance(target, NewEvent):
        return Event.objects.select_for_update().get(pk=target.pk)
    if target.season is not None:
        raise CloseOutError("Close Out records the scores in the active season; it doesn't start a new season.")
    if problem := new_event_problem(target):
        raise CloseOutError(problem)
    event = _create_event(target)
    event.open()
    _assign_school_list(event)
    # Sending needs an open event (begin_distribution), so only packets sent before events existed can match
    Packet.objects.sent().filter(event=None).update(event=event)
    return event


@dataclass(frozen=True)
class CloseOutOutcome:
    """The event Close Out froze and closed, how many school results it wrote and what the wipe deleted and reset."""

    event: Event
    results: int
    wiped: WipedData


# PostgreSQL's could-not-serialize error: a REPEATABLE READ transaction would change a row another one changed
# since its snapshot (a score, the config row, a ticket counter, ...)
SERIALIZATION_FAILURE = "40001"
CHANGED_WHILE_CLOSING_OUT = "Something changed while closing out; nothing was changed. Try again."


def close_out_event(target: Event | NewEvent, actor: str) -> CloseOutOutcome:
    """Close out the event (see _close_out). As the outermost transaction it runs at REPEATABLE READ, so the results
    it freezes, the archive and the rows it deletes are one snapshot: a score changed meanwhile makes it refuse with
    CHANGED_WHILE_CLOSING_OUT, writing nothing, and rows created after its snapshot stay as live data. Inside a caller's
    transaction, that transaction's isolation applies.
    """
    try:
        if connection.in_atomic_block:
            return _close_out(target, actor)
        # The lock first: inside the transaction it would come after the snapshot
        with school_list_held(), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return _close_out(target, actor)
    except OperationalError as e:
        if getattr(e.__cause__, "pgcode", None) == SERIALIZATION_FAILURE:
            raise CloseOutError(CHANGED_WHILE_CLOSING_OUT) from e
        raise


@transaction.atomic
def _close_out(target: Event | NewEvent, actor: str) -> CloseOutOutcome:
    """Freeze the event's results and export archive, then delete the live data, close the event, reset the schedule,
    start message, Quotient box list and queued announcements, and queue the Discord-only cleanup, all committed
    together. target is what the page showed: the open event, or with
    none open the new event to record the scores at. The school list stays until the next import replaces it.

    Under the school list lock and the competition config lock; CloseOutError, writing nothing, says why it refused.
    """
    school_list_lock()
    config = _lock_config()
    stage = event_stage()
    _refuse_a_stale_target(target, stage)
    if problem := close_out_problem(stage):
        raise CloseOutError(problem)
    event = _locked_target(target, stage)
    results = _freeze_results(event)
    wiped = wipe_competition_data()
    event.close()
    # The start message holds this event's credentials; the next event gets its own schedule and message
    config.competition_start_time = None
    config.competition_end_time = None
    config.start_message = ""
    config.save(update_fields=["competition_start_time", "competition_end_time", "start_message", "updated_at"])
    QuotientMetadataCache.objects.all().delete()
    announcements, _ = QueuedAnnouncement.objects.all().delete()
    # Committed with the close-out, so the bot never tears Discord down for a close-out that rolled back. The
    # database is reset here, so the bot does only the Discord side.
    DiscordTask.enqueue(CleanupCompetition(requested_by=actor, closed_out_event_id=event.pk))
    AuditLog.objects.create(
        action="event_closed_out",
        admin_user=actor,
        target_entity="event",
        target_id=event.pk,
        details={
            "event": event.name,
            "results": results,
            "deleted_counts": {k: v for k, v in wiped.deleted.items() if v},
            "total_deleted": sum(wiped.deleted.values()),
            "ticket_counters_reset": wiped.ticket_counters_reset,
            "queued_announcements_deleted": announcements,
        },
    )
    return CloseOutOutcome(event, results, wiped)


def _assign_school_list(event: Event) -> None:
    """Record each school on the list as holding its team at the new event; the list itself stays as it is."""
    EventTeamAssignment.objects.bulk_create(
        EventTeamAssignment(event=event, school_id=school_id, team_id=team_id)
        for school_id, team_id in SchoolInfo.objects.values_list("school_id", "team_id")
    )


def _school_by_team(event: Event | None) -> dict[int, int]:
    """Team id to school id at the event; with none, on the school list (what Close Out assigns a new event)."""
    holders = event.team_assignments if event is not None else SchoolInfo.objects
    return dict(holders.values_list("team_id", "school_id"))


@dataclass(frozen=True)
class SchoolSnapshot:
    """A school's live standing and its scorecard as Close Out would freeze it."""

    standing: Standing
    scorecard: dict[str, object]


def current_snapshots(event: Event | None, standings: list[Standing] | None = None) -> dict[int, SchoolSnapshot]:
    """Each school assigned at the event (with none, on the school list) whose team is active, by id, with its
    standing and scorecard from standings (by default, computed now)."""
    school_by_team = _school_by_team(event)
    if standings is None:
        standings = compute_standings()
    return {
        school_by_team[standing.team.pk]: SchoolSnapshot(
            standing, serialize_scorecard(build_scorecard_context(standing, standings))
        )
        for standing in standings
        if standing.team.pk in school_by_team
    }


@dataclass(frozen=True)
class TeamFix:
    """A team whose scores Close Out can't keep yet, and what the pages offer to fix it."""

    team_number: int
    activate: bool  # Inactive: Activate Team on its Teams page first
    enter_school: bool  # No school: enter it on its School Info page
    remove_school: bool  # Its school can be removed instead: no packets went out for the event


def _unfreezable(event: Event | None) -> tuple[str, list[TeamFix]]:
    """Why some live scores would have no result to keep them ('' if none would), and the teams named. Every school
    on the list must be on an active team (only active teams have standings), and every team with scores must hold a
    school."""
    school_by_team = _school_by_team(event)
    listed = Team.objects.filter(pk__in=school_by_team).order_by("team_number")
    if inactive := [team.team_number for team in listed if not team.is_active]:
        removable = not packets_sent(event)
        one = len(inactive) == 1
        problem = (
            f"{_team_list(inactive)} {'is' if one else 'are'} inactive but {'holds' if one else 'hold'} a school on "
            f"the list: activate {'it' if one else 'them'} with Activate Team on {'its' if one else 'their'} Teams "
            "page first" + (f", or remove {'its school' if one else 'their schools'}." if removable else ".")
        )
        return problem, [TeamFix(n, True, False, removable) for n in inactive[:SHOWN]]
    unlisted = list(scored_teams().exclude(pk__in=school_by_team).order_by("team_number"))
    if not unlisted:
        return "", []
    one = len(unlisted) == 1
    if not school_by_team:
        where = f"{event} has no schools on its list" if event is not None else "The school list is empty"
        problem = f"{where}, so there are no results to keep."
    else:
        numbers = _team_list([team.team_number for team in unlisted])
        problem = f"{numbers} {'has' if one else 'have'} scores but no school."
    enter = "enter its school on its School Info page" if one else "enter each team's school on its School Info page"
    if any(not team.is_active for team in unlisted):
        activate = "it" if one else "each inactive one"
        problem += f" Activate {activate} with Activate Team on its Teams page, then {enter} first."
    else:
        problem += f" {enter[0].upper()}{enter[1:]} first."
    return problem, [TeamFix(team.team_number, not team.is_active, True, False) for team in unlisted[:SHOWN]]


def _freeze_results(event: Event) -> int:
    """Freeze every listed school's scorecard and the export zip for the open event, replacing earlier ones.
    Returns how many results were written. Call under the school list lock with the event row locked, once
    close_out_problem has nothing to say."""
    snapshots = current_snapshots(event)
    EventResult.objects.filter(event=event).delete()
    EventResult.objects.bulk_create(
        EventResult(
            event=event,
            school_id=school_id,
            team_number=snapshot.standing.team.team_number,
            rank=snapshot.standing.rank,
            total_score=snapshot.standing.total_score,
            scorecard=snapshot.scorecard,
            version=SNAPSHOT_VERSION,
        )
        for school_id, snapshot in snapshots.items()
    )
    EventArchive.objects.update_or_create(event=event, defaults={"data": export_all_zip_bytes()})
    event.is_finalized = True
    event.save(update_fields=["is_finalized", "updated_at"])
    return len(snapshots)


# How many teams a refusal names
SHOWN = 3


def _team_list(numbers: list[int]) -> str:
    named = ", ".join(f"Team {number}" for number in numbers[:SHOWN])
    return f"{named} and {len(numbers) - SHOWN} more" if len(numbers) > SHOWN else named


def clear_leftovers_problem(stage: EventStage) -> str:
    """Why Clear leftover data can't run now, or ''. It deletes live data no open event holds: data with no scores,
    or scores once some event was closed out (before that, scores are an event that ran without an import)."""
    if stage.running:
        return _running_refusal(stage)
    if stage.event is not None:
        if stage.stage == Stage.READY:
            return f"{stage.event} is open. {stage.instruction(Action.CANCEL)} Then clear the data left over."
        return f"{stage.event} is open: its data goes when it is closed out. {stage.instruction(Action.CLOSE_OUT)}"
    if stage.stage == Stage.IDLE:
        return f"There is no leftover data to clear. {stage.next_step}"
    if stage.stage == Stage.UNRECORDED and not stage.closed_out_before:
        return f"These scores are not recorded at any event yet and no event was ever closed out. {stage.steps_left}"
    return ""


@transaction.atomic
def clear_leftover_data(actor: str) -> WipedData:
    """Delete live data no open event holds, unarchived. The schedule stays: a start may already be set for the
    next event. Under the school list lock and the competition config lock; CloseOutError when
    clear_leftovers_problem says why not."""
    school_list_lock()
    config = _lock_config()
    if problem := clear_leftovers_problem(event_stage()):
        raise CloseOutError(problem)
    wiped = wipe_competition_data()
    AuditLog.objects.create(
        action="leftover_data_cleared",
        admin_user=actor,
        target_entity="competition_config",
        target_id=config.pk,
        details={
            "deleted_counts": {k: v for k, v in wiped.deleted.items() if v},
            "total_deleted": sum(wiped.deleted.values()),
            "ticket_counters_reset": wiped.ticket_counters_reset,
        },
    )
    return wiped


def cancel_event_problem(stage: EventStage) -> str:
    """Why the open event can't be cancelled now, or ''. Only an event no team has scored: scores are closed out."""
    if stage.running:
        return _running_refusal(stage)
    if stage.event is None:
        return f"No event is open, so there is nothing to cancel. {stage.next_step}"
    if stage.stage != Stage.READY:
        return f"{stage.event} has scores, so it can't be cancelled. {stage.next_step}"
    return ""


@transaction.atomic
def cancel_event(target: Event, actor: str) -> Event:
    """Call off the open event before any team scored: close it and drop its assignments, so it can be deleted. The
    school list stays (the next import replaces it), and packets keep their event. Live data with no scores (Discord
    links, tickets) is then left over for Clear leftover data. Under the school list lock and the competition config
    lock; CloseOutError, writing nothing, says why it refused."""
    school_list_lock()
    _lock_config()
    stage = event_stage()
    if stage.event != target:
        raise CloseOutError(f"{target} is no longer open. Reload the page.")
    if problem := cancel_event_problem(stage):
        raise CloseOutError(problem)
    event = Event.objects.select_for_update().get(pk=target.pk)
    assignments, _ = event.team_assignments.all().delete()
    event.close()
    AuditLog.objects.create(
        action="event_cancelled",
        admin_user=actor,
        target_entity="event",
        target_id=event.pk,
        details={"event": event.name, "assignments_deleted": assignments},
    )
    return event


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
    if Packet.objects.sent().filter(event=event).exists():
        return f"Packets were sent for {event}; they are kept with it."
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
