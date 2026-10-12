from team.models import Team

from .models import Event, EventTeamAssignment, School, TeamRegistration


def assign_school_to_event(
    event: Event, team: Team, school_name: str, registration: TeamRegistration | None = None
) -> EventTeamAssignment:
    """Record that the school holds this team number at the event: the link the season history follows."""
    school = School.for_name(school_name)
    if registration is not None and registration.school_id != school.pk:
        registration.school = school
        registration.save(update_fields=["school"])
    return EventTeamAssignment.objects.create(event=event, team=team, school=school, registration=registration)
