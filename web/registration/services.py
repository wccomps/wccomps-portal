from team.models import Team

from .models import Event, EventTeamAssignment, School, TeamRegistration, normalize_school_name


def assign_school_to_event(
    event: Event, team: Team, school_name: str, registration: TeamRegistration | None = None
) -> EventTeamAssignment:
    """Record that the school holds this team number at the event: the link the season history follows."""
    school = School.for_name(school_name)
    if registration is not None and registration.school_id != school.pk:
        registration.school = school
        registration.save(update_fields=["school"])
    return EventTeamAssignment.objects.create(event=event, team=team, school=school, registration=registration)


class SchoolTakenError(Exception):
    """Another team in the event already holds the school; the message says which."""


def set_team_school(event: Event, team: Team, school_name: str) -> School:
    """Make the school the one holding this team at the event, keeping its registration on the same school.

    Returns that School; raises SchoolTakenError when another team in the event already holds it.
    """
    assignment = event.team_assignments.filter(team=team).select_related("school", "registration").first()
    if event.team_assignments.filter(school__name_key=normalize_school_name(school_name)).exclude(team=team).exists():
        raise SchoolTakenError(f"{school_name} already has a team in {event}.")
    if assignment is None:
        return assign_school_to_event(event, team, school_name).school
    school = School.for_name(school_name)
    if assignment.school_id != school.pk:
        assignment.school = school
        assignment.save(update_fields=["school"])
    registration = assignment.registration
    if registration is not None and registration.school_id != school.pk:
        registration.school = school
        registration.save(update_fields=["school"])
    return school
