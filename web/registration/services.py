import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from django.db import transaction
from django.db.models import Exists, OuterRef, QuerySet
from orange_team.models import OrangeAssignment
from packets.models import Packet
from scoring.calculator import Standing, compute_standings
from scoring.export import export_all_zip_bytes
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore, ServiceDetail, ServiceScore
from scoring.scorecard import build_scorecard_context
from scoring.snapshot import SNAPSHOT_VERSION, serialize_scorecard

from core.competition_utils import WipedData, live_competition_data_exists, wipe_competition_data
from core.discord_tasks import CleanupCompetition
from core.models import AuditLog, CompetitionConfig, DiscordTask
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
            f"Packets have already been sent for {event}. Finalize and clear the event instead, "
            "or change single schools on their School Info pages."
        )
    deleted, _ = SchoolInfo.objects.all().delete()
    if event is not None:
        event.team_assignments.all().delete()
    return deleted


class ImportBlockedError(Exception):
    """Why a school list can't be imported into or added to the event now."""


class LiveData(StrEnum):
    """Whose live competition data is in the portal while no event is open."""

    NONE = "none"  # an event is open (the data is its), or there is none
    LEFTOVERS = "leftovers"  # an event was closed out, so this arrived after the last close-out
    UNFINALIZED = "unfinalized"  # no event was ever finalized: an event that ran without an import


def live_data_state() -> LiveData:
    """What the live data is with no event open; Close Out, Clear leftover data, the import and the pages agree on
    it."""
    if Event.objects.open() is not None or not live_competition_data_exists():
        return LiveData.NONE
    return LiveData.LEFTOVERS if Event.objects.filter(is_finalized=True).exists() else LiveData.UNFINALIZED


def import_problem(event: Event) -> str:
    """Why this event can't be imported into now, or ''. An unsaved event is a new one."""
    open_now = Event.objects.open()
    if open_now and open_now != event:
        return (
            f"{open_now} is still open. Close it out (Close Out Event on the Competition page) before importing a "
            "list for another event."
        )
    if packets_sent(open_now):
        return (
            f"Packets have already been sent for {open_now}. Re-importing reassigns team numbers, so sent credentials "
            "would reach the wrong school. Change a single school on its School Info page instead. "
            "To add late schools, use Add schools."
        )
    if not live_competition_data_exists():
        return ""
    if open_now is None:
        if live_data_state() == LiveData.LEFTOVERS:
            return (
                "Live data arrived after the last event was closed out. Remove it with Clear leftover data on the "
                "competition Danger Zone page, then import the school list."
            )
        return (
            "Scores from the last event are still in the portal. "
            "Close that event out (Close Out Event on the Competition page) before importing a new school list."
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
    """Replace the school list with the rows and open the event (saving it first when new, and its season when new
    too, as the only active season), replacing its assignments: each row's school holds the row's team. Resets every
    team name and activates the rows' teams.

    Under the school list lock; the event is re-checked under it, and ImportBlockedError says why the import can't
    go ahead.
    """
    school_list_lock()
    if problem := import_problem(event):
        raise ImportBlockedError(problem)
    teams = Team.objects.in_bulk([row.team_number for row in rows], field_name="team_number")
    if missing := [str(row.team_number) for row in rows if row.team_number not in teams]:
        raise ImportBlockedError(f"These teams no longer exist: {', '.join(missing)}. Upload the file again.")
    if event.season._state.adding:
        event.season = _create_active_season(event.season)
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


def new_season_problem(season: Season) -> str:
    """Why the new season can't be created as the active one now, or ''."""
    if active := Season.objects.active():
        return f"The {active} season was activated since the preview. Upload the file again."
    if taken := Season.objects.filter(year=season.year).first():
        return f"The {taken} season already has the year {season.year}. Pick another year, or activate that season."
    return ""


def _create_active_season(season: Season) -> Season:
    """Save the new season as the only active one. Call under the school list lock; ImportBlockedError says why
    it can't be."""
    if problem := new_season_problem(season):
        raise ImportBlockedError(problem)
    season.is_active = True
    season.save()
    Season.objects.exclude(pk=season.pk).filter(is_active=True).update(is_active=False)
    return season


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


class CloseOutError(Exception):
    """Close Out Event or Clear leftover data refused; the message says why."""


@dataclass(frozen=True)
class NewEvent:
    """The event Close Out creates for the school list as it stands, for an event that ran without an import."""

    name: str
    date: date
    event_type: str


def _refuse_while_running(message: str) -> CompetitionConfig:
    """The competition config, its row locked as run_competition locks it, so a start can't land until commit.
    CloseOutError with the message while the competition runs."""
    config = CompetitionConfig.objects.select_for_update().get(pk=CompetitionConfig.get_config().pk)
    if config.applications_enabled:
        raise CloseOutError(message)
    return config


def _locked_target(target: Event | NewEvent) -> Event:
    """The open event the page showed, its row locked; or, with none open, new_event created and opened for the
    current school list. CloseOutError when the open event is no longer what the page showed. Call under the school
    list lock."""
    open_now = Event.objects.open()
    if isinstance(target, NewEvent):
        if open_now is not None:
            raise CloseOutError(f"{open_now} was opened since the page loaded. Reload the page.")
        event = _open_event_for_current_schools(target)
    elif open_now != target:
        raise CloseOutError(f"{target} is no longer open. Reload the page.")
    else:
        event = open_now
    return Event.objects.select_for_update().get(pk=event.pk)


@dataclass(frozen=True)
class CloseOutOutcome:
    """The event Close Out froze and closed, how many school results it wrote and what the wipe deleted and reset."""

    event: Event
    results: int
    wiped: WipedData


@transaction.atomic
def close_out_event(target: Event | NewEvent, actor: str) -> CloseOutOutcome:
    """Freeze the event's results and export archive, delete the live competition data, close the event, reset the
    competition's start and end times and queue the Discord cleanup for the bot, all in one transaction. target is
    what the page showed: the open event, or, with none open, the new event to create for the current school list.
    The school list stays until the next import replaces it.

    Under the school list lock and the competition config lock; CloseOutError, writing nothing, says why it refused.
    """
    school_list_lock()
    config = _refuse_while_running("Stop the competition first.")
    event = _locked_target(target)
    results = _freeze_results(event)
    wiped = wipe_competition_data()
    event.close()
    config.competition_start_time = None
    config.competition_end_time = None
    config.save(update_fields=["competition_start_time", "competition_end_time", "updated_at"])
    # Committed with the close-out, so the bot never tears Discord down for a close-out that rolled back
    DiscordTask.enqueue(CleanupCompetition(requested_by=actor))
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
        },
    )
    return CloseOutOutcome(event, results, wiped)


def _open_event_for_current_schools(new_event: NewEvent) -> Event:
    """Create and open new_event in the active season for the school list as it stands: each school on the list is
    assigned its team at it, and packets sent while no event was open are stamped with it."""
    state = live_data_state()
    if state == LiveData.LEFTOVERS:
        raise CloseOutError(
            "Live data after the last close-out is leftovers: use Clear leftover data on the Danger Zone page, "
            "then import the next school list."
        )
    if state == LiveData.NONE:
        raise CloseOutError("Nothing to close out: no live scores. Import the next school list to open an event.")
    season = Season.objects.active()
    if season is None:
        raise CloseOutError("There is no active season to hold the event. Activate a season first.")
    if Event.objects.filter(season=season, name=new_event.name).exists():
        raise CloseOutError(f"The {season} season already has an event named {new_event.name}. Choose another name.")
    event = Event.objects.create(
        season=season,
        name=new_event.name,
        date=new_event.date,
        event_type=new_event.event_type,
        # Schools come from the list, not the public registration form
        registration_open=False,
    )
    event.open()
    _assign_school_list(event)
    # Sending needs an open event (begin_distribution), so only packets sent before events existed can match
    Packet.objects.sent().filter(event=None).update(event=event)
    return event


def _assign_school_list(event: Event) -> None:
    """Record each school on the list as holding its team at the new event; the list itself stays as it is."""
    EventTeamAssignment.objects.bulk_create(
        EventTeamAssignment(event=event, school_id=school_id, team_id=team_id)
        for school_id, team_id in SchoolInfo.objects.values_list("school_id", "team_id")
    )


@dataclass(frozen=True)
class SchoolSnapshot:
    """A school's live standing and its scorecard as Close Out would freeze it."""

    standing: Standing
    scorecard: dict[str, object]


def current_snapshots(event: Event | None) -> dict[int, SchoolSnapshot]:
    """Each school assigned at the event whose team is active, by id, with its standing and scorecard now. With no
    event, the schools on the school list, which an event created at close-out is assigned."""
    holders = event.team_assignments if event is not None else SchoolInfo.objects
    school_by_team = dict(holders.values_list("team_id", "school_id"))
    standings = compute_standings()
    return {
        school_by_team[standing.team.pk]: SchoolSnapshot(
            standing, serialize_scorecard(build_scorecard_context(standing, standings))
        )
        for standing in standings
        if standing.team.pk in school_by_team
    }


def scorecards_fingerprint(event: Event | None) -> str:
    """A hash of every school's scorecard as current_snapshots gives it now, to tell later whether the scorecards
    emailed then still say what the scores say."""
    scorecards = {
        school_id: (snapshot.standing.team.team_number, snapshot.scorecard)
        for school_id, snapshot in current_snapshots(event).items()
    }
    # Through JSON first, as the stored results go: int dict keys become strings, so the keys sort
    normalized = json.dumps(json.loads(json.dumps(scorecards)), sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _counted(count: int, one: str, many: str) -> list[str]:
    """The sentence for count things, or none when there are none."""
    if not count:
        return []
    return [f"{count} {one if count == 1 else many}."]


def close_out_warnings(event: Event | None) -> list[str]:
    """Unfinished work a close-out of the event (None: the event it creates) would freeze as it stands, as sentences
    for the page to show before confirming. They never block the close-out."""
    open_tickets = Ticket.objects.filter(status__in=[Ticket.STATUS_OPEN, Ticket.STATUS_CLAIMED]).count()
    unapproved_tickets = Ticket.objects.filter(status=Ticket.STATUS_RESOLVED, is_approved=False).count()
    unscored = OrangeAssignment.objects.exclude(status="approved").count()
    warnings = [
        *_counted(
            InjectScore.objects.filter(is_approved=False).count(),
            "inject grade is not approved",
            "inject grades are not approved",
        ),
        *_counted(open_tickets, "ticket is not resolved", "tickets are not resolved"),
        *_counted(unapproved_tickets, "resolved ticket is not approved", "resolved tickets are not approved"),
        *_counted(
            IncidentReport.objects.filter(is_approved=False).count(),
            "incident report is not reviewed",
            "incident reports are not reviewed",
        ),
        *_counted(
            RedTeamScore.objects.filter(is_approved=False).count(),
            "red team finding is not approved",
            "red team findings are not approved",
        ),
        *_counted(
            OrangeTeamScore.objects.filter(is_approved=False).count(),
            "orange check score is not approved",
            "orange check scores are not approved",
        ),
        *_counted(
            unscored, "orange assignment is not scored and approved", "orange assignments are not scored and approved"
        ),
    ]
    if event is not None and not live_competition_data_exists():
        warnings.append("No team has live scores yet, so the results will be empty.")
    return warnings + _scorecard_warnings(event)


def _scorecard_warnings(event: Event | None) -> list[str]:
    """Whether the last Email Scorecards for the event sent the scorecards as they stand now."""
    emailed = (
        AuditLog.objects.filter(action="scorecards_emailed", details__event=event.pk if event else None)
        .order_by("-created_at", "-pk")
        .first()
    )
    if emailed is None:
        return ["Scorecards haven't been emailed for this event."]
    warnings = _counted(
        emailed.details.get("failed", 0),
        "scorecard email failed in the last send",
        "scorecard emails failed in the last send",
    )
    if emailed.details.get("scorecards_hash") != scorecards_fingerprint(event):
        warnings.append("Scores changed after the last scorecard email.")
    return warnings


def _scored_teams() -> QuerySet[Team]:
    """Teams with any live score row: what the close-out would delete for them."""
    team = OuterRef("pk")
    return Team.objects.filter(
        Exists(ServiceScore.objects.filter(team=team))
        | Exists(ServiceDetail.objects.filter(team=team))
        | Exists(InjectScore.objects.filter(team=team))
        | Exists(OrangeTeamScore.objects.filter(team=team))
        | Exists(IncidentReport.objects.filter(team=team))
        | Exists(RedTeamScore.objects.filter(affected_teams=team))
    )


def _unfreezable_problem(event: Event) -> str:
    """Why some live scores would have no result to keep them, or ''. Every school on the list must be on an active
    team (only active teams have standings), and every team with scores must hold a school."""
    assigned = event.team_assignments.select_related("team").order_by("team__team_number")
    if not assigned:
        return f"{event} has no schools on its list, so there are no results to keep."
    if inactive := [a.team.team_number for a in assigned if not a.team.is_active]:
        theirs = "its school" if len(inactive) == 1 else "their schools"
        return f"Reactivate {_team_list(inactive)} or remove {theirs} first."
    unlisted = list(
        _scored_teams()
        .exclude(pk__in=[a.team_id for a in assigned])
        .order_by("team_number")
        .values_list("team_number", flat=True)
    )
    if unlisted:
        has, theirs = ("has", "its school") if len(unlisted) == 1 else ("have", "their schools")
        return f"{_team_list(unlisted)} {has} scores but no school: put {theirs} on the list first."
    return ""


def _freeze_results(event: Event) -> int:
    """Freeze every listed school's scorecard and the export zip for the open event, replacing earlier ones.
    Returns how many results were written. Call under the school list lock with the event row locked."""
    if problem := _unfreezable_problem(event):
        raise CloseOutError(problem)
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


def _team_list(numbers: list[int], shown: int = 3) -> str:
    named = ", ".join(f"Team {number}" for number in numbers[:shown])
    return f"{named} and {len(numbers) - shown} more" if len(numbers) > shown else named


@transaction.atomic
def clear_leftover_data(actor: str) -> WipedData:
    """Delete live data that arrived after the last close-out, with no event open. The schedule stays: a start may
    already be set for the next event. Under the school list lock and the competition config lock; CloseOutError
    unless it is leftovers."""
    school_list_lock()
    config = _refuse_while_running("Competition must be stopped before clearing")
    state = live_data_state()
    if state == LiveData.UNFINALIZED:
        raise CloseOutError("This live data belongs to an event that was never closed out: use Close Out Event.")
    if state == LiveData.NONE:
        if open_now := Event.objects.open():
            raise CloseOutError(f"{open_now} is open: its data goes when it is closed out (Close Out Event).")
        raise CloseOutError("There is no leftover data to clear.")
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
