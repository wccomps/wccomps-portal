"""A school is one record across events, matched by name ignoring case and spacing."""

import re
from datetime import date, timedelta

import pytest
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from scoring.models import ServiceScore

from registration.models import (
    Event,
    EventResult,
    EventTeamAssignment,
    RegistrationContact,
    RegistrationEventEnrollment,
    School,
    Season,
    TeamRegistration,
    normalize_school_name,
)
from registration.services import set_team_school
from team.models import SchoolInfo, Team

# The Unassign button (a c-button, so its label sits between whitespace)
UNASSIGN = re.compile(r">\s*Unassign\s*<")

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


@pytest.mark.parametrize(
    "name", ["Example University", "example university", "  Example   University ", "EXAMPLE\tUNIVERSITY"]
)
def test_normalize_ignores_case_and_spacing(name):
    assert normalize_school_name(name) == "example university"


def test_for_name_matches_an_existing_school_and_keeps_the_latest_spelling():
    first = School.for_name("Example university")

    again = School.for_name("  Example   University ")

    assert again.pk == first.pk
    assert School.objects.get().name == "Example University"


def test_one_team_per_school_per_event(event):
    team1 = Team.objects.create(team_number=1, team_name="Team 01")
    team2 = Team.objects.create(team_number=2, team_name="Team 02")
    EventTeamAssignment.objects.create(event=event, team=team1, school=School.for_name("Example University"))

    with pytest.raises(IntegrityError):
        EventTeamAssignment.objects.create(event=event, team=team2, school=School.for_name("example university"))


def test_unassigning_an_imported_school_without_a_registration(gold_team_user, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="c@example.edu")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_unassign_team", args=[EventTeamAssignment.objects.get().pk]), _shown())

    assert response.status_code == 302
    assert not EventTeamAssignment.objects.exists()
    assert not SchoolInfo.objects.exists()


def _shown():
    """The school the event page showed for the one assignment, posted back with the Unassign form."""
    return {"school": EventTeamAssignment.objects.get().school_id}


def _enrolled(event, school_name, *contacts):
    """An approved registration enrolled in the event, with (role, email) contacts; a captain by default."""
    registration = TeamRegistration.objects.create(school_name=school_name, status="approved")
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)
    for role, email in contacts or [("captain", "captain@example.edu")]:
        RegistrationContact.objects.create(registration=registration, role=role, name=role, email=email)
    return registration


def test_event_detail_shows_an_imported_assignment_for_an_enrolled_school(gold_team_user, event):
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    imported = EventTeamAssignment.objects.get()
    _enrolled(event, "example university")
    client = Client()
    client.force_login(gold_team_user)

    response = client.get(reverse("registration_event_detail", args=[event.pk]))

    [item] = response.context["rows"]
    assert item.assignment == imported
    assert item.can_assign is False
    assert b"Team 04" in response.content


def test_saved_school_must_be_reconciled_not_randomly_assigned_or_unassigned(gold_team_user, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    Team.objects.create(team_number=5, team_name="Team 05", is_active=False)
    school = School.for_name("Saved school")
    _enrolled(event, school.name)
    set_team_school(team, school.name, contact_email="c@example.edu")
    assignment = event.team_assignments.get(school=school)
    EventResult.objects.create(event=event, school=school, team_number=4, total_score=0, scorecard={})
    client = Client()
    client.force_login(gold_team_user)
    url = reverse("registration_event_detail", args=[event.pk])

    assert not UNASSIGN.search(client.get(url).content.decode())
    reply = client.post(reverse("registration_unassign_team", args=[assignment.pk]), {"school": school.pk}, follow=True)
    assert b"Saved scorecards need their school and team number kept together" in reply.content
    assert event.team_assignments.filter(pk=assignment.pk).exists()
    assignment.delete()
    assert SchoolInfo.objects.filter(school=school, team=team).exists()
    detail = client.get(url).content.decode()
    assert "<strong>0</strong> ready to assign" in detail and "has a saved card for Team 4" in detail
    assert not re.search(r">\s*Ready to assign\s*<", detail)
    assert "Assign Team Numbers" not in detail
    reply = client.post(reverse("registration_assign_teams", args=[event.pk]), follow=True)
    assert b"has a saved card for Team 4" in reply.content
    assert not event.team_assignments.exists()
    assert not Team.objects.filter(is_active=True, team_number=5).exists()


def test_assign_teams_skips_a_school_that_already_has_a_team(gold_team_user, event):
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "example university")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert response.status_code == 302
    assert EventTeamAssignment.objects.filter(event=event).count() == 1


def test_assign_teams_assigns_none_of_two_approved_registrations_of_the_same_school(gold_team_user, event):
    """Not first-wins: the earlier one could be a student's registration of a rival school, with the student's email."""
    Team.objects.create(team_number=4, team_name="Team 04")
    Team.objects.create(team_number=5, team_name="Team 05")
    Team.objects.create(team_number=6, team_name="Team 06")
    impostor = _enrolled(event, "Example University", ("captain", "student@example.com"))
    real = _enrolled(event, "example  university", ("captain", "captain@example.edu"))
    _enrolled(event, "Other College", ("captain", "other@example.edu"))
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert list(EventTeamAssignment.objects.values_list("school__name", flat=True)) == ["Other College"]
    assert list(SchoolInfo.objects.values_list("contact_email", flat=True)) == ["other@example.edu"]
    content = response.content.decode()
    assert "Assigned 1 team" in content
    assert "Not assigned: Example University: more than one approved registration names that school." in content

    # Staff reject the impostor (approved, so rejectable only as one of two), then assign again
    client.post(reverse("registration_reject", args=[impostor.pk]), {"reason": "Not the school"})
    client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert EventTeamAssignment.objects.get(school__name_key="example university").registration == real
    assert SchoolInfo.objects.get(school__name_key="example university").contact_email == "captain@example.edu"


def test_assign_teams_adds_each_school_to_the_school_list_with_its_captain(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    registration = _enrolled(
        event, "Example University", ("coach", "coach@example.edu"), ("captain", "captain@example.edu")
    )
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_assign_teams", args=[event.pk]))

    info = SchoolInfo.objects.get()
    assignment = EventTeamAssignment.objects.get(event=event)
    assert info.team == assignment.team
    assert info.school == assignment.school
    assert info.contact_email == "captain@example.edu"
    assert (info.secondary_email, info.notes) == ("", "")
    # The School Info page says who last changed it
    assert info.updated_by == gold_team_user.get_username()
    assert assignment.registration == registration


def test_assign_teams_uses_the_first_contact_without_a_captain(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(event, "Example University", ("coach", "coach@example.edu"), ("site_judge", "judge@example.edu"))
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert SchoolInfo.objects.get().contact_email == "coach@example.edu"


def _no_contact(event, school_name):
    registration = TeamRegistration.objects.create(school_name=school_name, status="approved")
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)


def test_assign_teams_skips_a_registration_with_no_contact_email(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    _no_contact(event, "Example University")

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert not SchoolInfo.objects.exists()
    assert not EventTeamAssignment.objects.exists()
    assert "Skipped Example University: no contact email on the registration." in response.content.decode()


def test_assign_teams_with_too_few_free_teams_assigns_none_and_says_how_many_it_needs(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(event, "Example University")
    _enrolled(event, "Other College")
    _no_contact(event, "Third College")

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    content = response.content.decode()
    assert "Not enough teams available. Need 2, have 1." in content
    assert "Skipped Third College: no contact email on the registration." in content
    assert not SchoolInfo.objects.exists()
    assert not EventTeamAssignment.objects.exists()


def test_assign_teams_says_how_many_teams_it_assigned(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "Example University")
    _enrolled(event, "Other College")

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert "Assigned 2 teams successfully." in response.content.decode()
    assert SchoolInfo.objects.count() == 2


def test_assign_teams_skips_a_school_already_in_the_school_list(gold_team_user, event):
    event.is_active = False
    event.save()
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    event.open()
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "example university")
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert SchoolInfo.objects.count() == 1
    assert not EventTeamAssignment.objects.exists()


def test_assign_teams_names_every_school_it_skipped_for_already_having_a_team(gold_team_user, event):
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    set_team_school(
        Team.objects.create(team_number=5, team_name="Team 05"), "Other College", contact_email="d@example.edu"
    )
    _enrolled(event, "Example University")
    _enrolled(event, "Other College")

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert [str(m) for m in response.context["messages"]] == [
        "Skipped Example University, Other College: that school already has a team.",
        "No eligible registrations to assign.",
    ]


def test_assign_teams_names_the_schools_with_no_contact_in_the_order_they_enrolled(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    _no_contact(event, "Example University")
    _no_contact(event, "Other College")
    # Other College enrolled first, though its row was written second
    RegistrationEventEnrollment.objects.filter(registration__school_name="Other College").update(
        enrolled_at=timezone.now() - timedelta(days=1)
    )

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert [str(m) for m in response.context["messages"]] == [
        "Skipped Other College, Example University: no contact email on the registration.",
        "No eligible registrations to assign.",
    ]


def test_assign_teams_names_every_school_registered_twice_and_says_what_to_do(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    for name in ["Example University", "example university", "Other College", "OTHER COLLEGE"]:
        _enrolled(event, name)

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert [str(m) for m in response.context["messages"]] == [
        "Not assigned: Example University, Other College: more than one approved registration names that school. "
        "Reject the ones that aren't the school's on the Registrations page, then assign again.",
        "No eligible registrations to assign.",
    ]
    assert not SchoolInfo.objects.exists()


def test_assign_teams_assigns_only_registrations_enrolled_in_the_open_event(gold_team_user, event):
    other = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    Team.objects.create(team_number=4, team_name="Team 04")
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "Example University")
    _enrolled(other, "Other College")

    _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]))

    assert list(SchoolInfo.objects.values_list("school__name", flat=True)) == ["Example University"]


def test_assign_teams_is_refused_for_an_event_that_is_not_open(gold_team_user, event):
    other = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(other, "Example University")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[other.pk]), follow=True)

    assert [str(m) for m in response.context["messages"]] == [
        "Teams can only be assigned for the open event: import its school list first."
    ]
    assert not SchoolInfo.objects.exists()
    assert not EventTeamAssignment.objects.exists()


def test_unassigning_an_earlier_events_team_is_refused(gold_team_user, event):
    past = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    team = Team.objects.create(team_number=4, team_name="Team 04")
    history = EventTeamAssignment.objects.create(event=past, team=team, school=School.for_name("Old College"))
    set_team_school(team, "Example University", contact_email="c@example.edu")
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_unassign_team", args=[history.pk]), {"school": history.school_id})

    assert EventTeamAssignment.objects.filter(pk=history.pk).exists()
    assert SchoolInfo.objects.filter(team=team).exists()


def _client(user):
    client = Client()
    client.force_login(user)
    return client


def _sent_packet(event):
    from packets.models import Packet

    return Packet.objects.create(
        title="Packet",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        uploaded_by="gold",
        status="completed",
        event=event,
    )


def test_assign_teams_activates_the_team_it_picks(gold_team_user, event):
    team = Team.objects.create(team_number=4, team_name="Team 04", is_active=False)
    _enrolled(event, "Example University")

    _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]))

    team.refresh_from_db()
    assert team.is_active
    assert SchoolInfo.objects.get().team == team


def test_assign_teams_picks_only_teams_with_no_school_in_the_list(gold_team_user, event):
    event.is_active = False
    event.save()
    held = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(held, "Other College", contact_email="o@example.edu")
    event.open()
    scored = Team.objects.create(team_number=3, team_name="Team 03", is_active=False)
    ServiceScore.objects.create(team=scored, service_points=5)
    mapped = Team.objects.create(team_number=6, team_name="Team 06", is_active=False)
    set_team_school(mapped, "Mapped College", contact_email="m@example.edu")
    ServiceScore.objects.create(team=mapped, service_points=10)
    free = Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "Example University")

    _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]))

    assert EventTeamAssignment.objects.get(event=event, school__name="Example University").team == free
    assert not event.team_assignments.filter(team=scored).exists()
    assert not event.team_assignments.filter(team=held).exists()
    assert SchoolInfo.objects.get(team=held).school.name == "Other College"
    assert event.team_assignments.get(team=mapped).school.name == "Mapped College"
    assert ServiceScore.objects.get(team=mapped).service_points == 10
    scored.refresh_from_db()
    assert not scored.is_active and ServiceScore.objects.get(team=scored).service_points == 5


@pytest.mark.parametrize("failure", ["taken", "integrity"])
def test_assign_teams_skips_a_registration_that_fails_and_assigns_the_rest(gold_team_user, event, monkeypatch, failure):
    from registration import views
    from registration.services import SchoolTakenError

    Team.objects.create(team_number=4, team_name="Team 04")
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "Example University")
    _enrolled(event, "Other College")
    real = views.set_team_school

    def flaky(team, school_name, **kwargs):
        if school_name == "Example University":
            set_team_school(team, school_name, **kwargs)
            raise SchoolTakenError("taken") if failure == "taken" else IntegrityError("clash")
        return real(team, school_name, **kwargs)

    monkeypatch.setattr(views, "set_team_school", flaky)

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert response.status_code == 200
    assert list(SchoolInfo.objects.values_list("school__name", flat=True)) == ["Other College"]
    assert list(EventTeamAssignment.objects.values_list("school__name", flat=True)) == ["Other College"]
    content = response.content.decode()
    assert "Assigned 1 team" in content
    assert "Skipped Example University: that school already has a team." in content


def test_unassign_is_refused_once_a_packet_went_out_for_the_open_event(gold_team_user, event):
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    _sent_packet(event)

    response = _client(gold_team_user).post(
        reverse("registration_unassign_team", args=[EventTeamAssignment.objects.get().pk]), _shown(), follow=True
    )

    assert f"Packets have already been sent for {event}" in response.content.decode()
    assert EventTeamAssignment.objects.exists()
    assert SchoolInfo.objects.exists()


def test_unassign_goes_ahead_when_only_an_earlier_events_packet_went_out(gold_team_user, event):
    past = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    _sent_packet(past)
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )

    _client(gold_team_user).post(
        reverse("registration_unassign_team", args=[EventTeamAssignment.objects.get().pk]), _shown()
    )

    assert not EventTeamAssignment.objects.exists()
    assert not SchoolInfo.objects.exists()


def test_event_detail_offers_no_unassign_once_packets_went_out(gold_team_user, event):
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"), "Example University", contact_email="c@example.edu"
    )
    _enrolled(event, "Example University")
    url = reverse("registration_event_detail", args=[event.pk])
    client = _client(gold_team_user)
    before = client.get(url).content.decode()
    _sent_packet(event)

    after = client.get(url).content.decode()

    assert UNASSIGN.search(before)
    assert not UNASSIGN.search(after)
    assert "Packets sent" in after


def test_unassign_is_refused_when_the_school_changed_since_the_page_was_opened(gold_team_user, event):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="c@example.edu")
    _enrolled(event, "Example University")
    client = _client(gold_team_user)
    page = client.get(reverse("registration_event_detail", args=[event.pk])).content.decode()
    shown = _shown()
    set_team_school(team, "Other College", contact_email="o@example.edu")

    response = client.post(
        reverse("registration_unassign_team", args=[EventTeamAssignment.objects.get().pk]), shown, follow=True
    )

    assert f'name="school" value="{shown["school"]}"' in page
    assert "Team 4&#x27;s school changed since you opened this page; reload." in response.content.decode()
    assert SchoolInfo.objects.get().school_name == "Other College"
    assert EventTeamAssignment.objects.get().school.name == "Other College"


def test_assign_teams_skips_a_team_that_got_a_school_after_it_was_picked(gold_team_user, event, monkeypatch):
    from registration import views

    team = Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(event, "Example University")

    def school_set_meanwhile(teams):
        # Between the pick and the assignment: another tab gives the picked team a school
        set_team_school(team, "Other College", contact_email="o@example.edu")

    monkeypatch.setattr(views.random, "shuffle", school_set_meanwhile)

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert SchoolInfo.objects.get(team=team).school_name == "Other College"
    assert list(EventTeamAssignment.objects.values_list("school__name", flat=True)) == ["Other College"]
    assert "Skipped Example University: the team picked for it got a school meanwhile" in response.content.decode()


def test_assign_teams_names_every_school_whose_picked_team_got_a_school_meanwhile(gold_team_user, event, monkeypatch):
    from registration import views

    teams = [Team.objects.create(team_number=n, team_name=f"Team 0{n}") for n in (4, 5)]
    _enrolled(event, "Example University")
    _enrolled(event, "Second College")

    def schools_set_meanwhile(picked):
        for team, name in zip(teams, ["Other College", "Third College"], strict=True):
            set_team_school(team, name, contact_email="o@example.edu")

    monkeypatch.setattr(views.random, "shuffle", schools_set_meanwhile)

    response = _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert [str(m) for m in response.context["messages"]] == [
        "Skipped Example University, Second College: the team picked for it got a school meanwhile. Assign again."
    ]


def test_event_detail_notes_sent_packets_once_for_the_event(gold_team_user, event):
    for number, name in [(4, "Example University"), (5, "Other College")]:
        set_team_school(
            Team.objects.create(team_number=number, team_name=f"Team {number:02d}"), name, contact_email="c@e.edu"
        )
        _enrolled(event, name)
    _sent_packet(event)

    content = _client(gold_team_user).get(reverse("registration_event_detail", args=[event.pk])).content.decode()

    assert content.count("Packets sent") == 1


def test_event_detail_offers_no_unassign_for_an_earlier_event(gold_team_user, event):
    past = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    registration = _enrolled(past, "Example University")
    team = Team.objects.create(team_number=4, team_name="Team 04")
    EventTeamAssignment.objects.create(
        event=past, team=team, school=School.for_name("Example University"), registration=registration
    )

    content = _client(gold_team_user).get(reverse("registration_event_detail", args=[past.pk])).content.decode()

    assert "Team 04" in content
    assert not UNASSIGN.search(content)


def test_event_detail_lists_the_schools_an_imported_event_had(gold_team_user, event):
    """A past event with imported assignments and no enrollments lists its teams from the assignments."""
    past = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    for number, name in [(7, "Other College"), (4, "Example University")]:
        team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}")
        EventTeamAssignment.objects.create(event=past, team=team, school=School.for_name(name))

    response = _client(gold_team_user).get(reverse("registration_event_detail", args=[past.pk]))

    assert [(row.assignment.school.name, row.registration) for row in response.context["rows"]] == [
        ("Example University", None),
        ("Other College", None),
    ]
    content = response.content.decode()
    assert "No registration" in content
    assert "Team 04" in content
    assert "Team 07" in content
    assert not UNASSIGN.search(content)


def test_event_detail_shows_the_contact_assignment_emails(gold_team_user, event):
    """With no captain on record, assignment emails the coach: the Contact column names the same person."""
    _enrolled(event, "Example University", ("coach", "coach@example.edu"))

    content = _client(gold_team_user).get(reverse("registration_event_detail", args=[event.pk])).content.decode()

    assert "coach@example.edu" in content


def test_unassigning_a_team_with_linked_members_is_refused(gold_team_user, event):
    from django.contrib.auth.models import User

    from registration.services import LINKED_TEAM_REMOVE
    from team.models import DiscordLink

    team = Team.objects.create(team_number=4, team_name="Team 04")
    set_team_school(team, "Example University", contact_email="c@example.edu")
    user = User.objects.create(username="member04")
    DiscordLink.objects.create(discord_id=44, discord_username="m", user=user, team=team, is_active=True)
    client = Client()
    client.force_login(gold_team_user)
    assert not UNASSIGN.search(client.get(reverse("registration_event_detail", args=[event.pk])).content.decode())

    response = client.post(
        reverse("registration_unassign_team", args=[EventTeamAssignment.objects.get().pk]), _shown(), follow=True
    )

    assert LINKED_TEAM_REMOVE in [str(m) for m in response.context["messages"]]
    assert EventTeamAssignment.objects.exists()
    assert DiscordLink.objects.filter(is_active=True).exists()
