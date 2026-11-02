"""A school is one record across events, matched by name ignoring case and spacing."""

import re
from datetime import date

import pytest
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse

from registration.models import (
    Event,
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

    [item] = response.context["enrollment_data"]
    assert item["assignment"] == imported
    assert item["can_assign"] is False
    assert b"Team 04" in response.content


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


def test_assign_teams_gives_one_team_to_registrations_of_the_same_school(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "Example University")
    _enrolled(event, "example  university")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert EventTeamAssignment.objects.filter(event=event).count() == 1
    content = response.content.decode()
    assert "Assigned 1 team" in content
    assert "already has a team" in content


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
    assert assignment.registration == registration


def test_assign_teams_uses_the_first_contact_without_a_captain(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(event, "Example University", ("coach", "coach@example.edu"), ("site_judge", "judge@example.edu"))
    client = Client()
    client.force_login(gold_team_user)

    client.post(reverse("registration_assign_teams", args=[event.pk]))

    assert SchoolInfo.objects.get().contact_email == "coach@example.edu"


def test_assign_teams_skips_a_registration_with_no_contact_email(gold_team_user, event):
    Team.objects.create(team_number=4, team_name="Team 04")
    registration = TeamRegistration.objects.create(school_name="Example University", status="approved")
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[event.pk]), follow=True)

    assert not SchoolInfo.objects.exists()
    assert not EventTeamAssignment.objects.exists()
    assert "Example University: no contact email" in response.content.decode()


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


def test_assign_teams_is_refused_for_an_event_that_is_not_open(gold_team_user, event):
    other = Event.objects.create(season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 1, 3))
    Team.objects.create(team_number=4, team_name="Team 04")
    _enrolled(other, "Example University")
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("registration_assign_teams", args=[other.pk]), follow=True)

    assert "only be assigned for the open event" in response.content.decode()
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
    free = Team.objects.create(team_number=5, team_name="Team 05")
    _enrolled(event, "Example University")

    _client(gold_team_user).post(reverse("registration_assign_teams", args=[event.pk]))

    assert EventTeamAssignment.objects.get(event=event, school__name="Example University").team == free


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

    response = client.post(
        reverse("registration_unassign_team", args=[EventTeamAssignment.objects.get().pk]), _shown(), follow=True
    )

    assert LINKED_TEAM_REMOVE in [str(m) for m in response.context["messages"]]
    assert EventTeamAssignment.objects.exists()
    assert DiscordLink.objects.filter(is_active=True).exists()
