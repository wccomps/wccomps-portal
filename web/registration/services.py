from django.db import transaction

from team.models import SchoolInfo, Team

from .models import Event, EventTeamAssignment, School, TeamRegistration


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

    The only writer of a team's school: the open event's assignment, and the registration given or already linked
    to it, follow the school list. Raises SchoolTakenError when another team holds the school.
    """
    # Locked so two teams claiming one school at the same time can't both pass the check
    school = School.objects.select_for_update().get(pk=School.for_name(school_name).pk)
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
            assignment.school = school
            assignment.registration = registration or assignment.registration
            assignment.save(update_fields=["school", "registration"])
        registration = assignment.registration
    if registration is not None and registration.school_id != school.pk:
        registration.school = school
        registration.save(update_fields=["school"])
    return info


@transaction.atomic
def remove_team_school(team: Team) -> None:
    """Take the team out of the school list and out of the open event; earlier events keep their assignments."""
    SchoolInfo.objects.filter(team=team).delete()
    event = Event.objects.open()
    if event is not None:
        event.team_assignments.filter(team=team).delete()
