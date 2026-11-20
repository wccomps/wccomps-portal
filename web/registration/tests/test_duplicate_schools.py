"""A student can register a rival school's name with their own email. The review page flags a second registration of
a school for the same event, Approve refuses one while another is approved, and only then is an approved one
rejectable (assign_teams refusing both is in test_school)."""

from datetime import date

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from registration.models import (
    Event,
    RegistrationContact,
    RegistrationEventEnrollment,
    Season,
    TeamRegistration,
)
from registration.services import set_team_school
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _registration(event, school_name, email, status="pending"):
    registration = TeamRegistration.objects.create(school_name=school_name, status=status)
    RegistrationContact.objects.create(registration=registration, role="captain", name="Captain", email=email)
    RegistrationEventEnrollment.objects.create(registration=registration, event=event)
    return registration


def _rows(client):
    return dict(client.get(reverse("registration_review_list")).context["registrations"])


def test_the_review_page_flags_registrations_of_the_same_school_for_an_event(client, event):
    real = _registration(event, "Example University", "captain@example.edu", status="approved")
    impostor = _registration(event, "example   UNIVERSITY", "student@example.com")
    lone = _registration(event, "Other College", "other@example.edu")

    response = client.get(reverse("registration_review_list"))

    rows = dict(response.context["registrations"])
    assert rows[lone] is None
    assert rows[impostor].same_school == [real]
    assert rows[real].same_school == [impostor]
    content = response.content.decode()
    assert content.count("Duplicate school") == 2
    assert "Also registered by captain@example.edu (Approved)" in content
    assert "Also registered by student@example.com (Pending Review)" in content


@pytest.mark.parametrize("other", ["rejected", "an earlier season", "another event"])
def test_the_review_page_does_not_flag_a_school_registered_once_for_an_event(client, event, other):
    """A rejected registration is no clash, and a school registers again each season, or for another event."""
    if other == "rejected":
        _registration(event, "Example University", "captain@example.edu", status="rejected")
    elif other == "an earlier season":
        season = Season.objects.create(name="2025", year=2025)
        earlier = Event.objects.create(
            season=season, name="Invitational", event_type="invitational", date=date(2025, 10, 4), is_finalized=True
        )
        _registration(earlier, "Example University", "captain@example.edu", status="approved")
    else:
        qualifier = Event.objects.create(
            season=event.season, name="Qualifier", event_type="qualifier", date=date(2026, 11, 7)
        )
        _registration(qualifier, "Example University", "captain@example.edu", status="approved")
    registration = _registration(event, "Example University", "captain@example.edu")

    assert _rows(client)[registration] is None


def test_the_review_page_flags_a_school_a_team_already_holds_but_not_for_the_registration_it_was_given_to(
    client, event
):
    team = Team.objects.create(team_number=4, team_name="Team 04")
    given = _registration(event, "Example University", "captain@example.edu", status="approved")
    set_team_school(team, "Example University", contact_email="captain@example.edu", registration=given)
    imported = Team.objects.create(team_number=7, team_name="Team 07")
    set_team_school(imported, "Other College", contact_email="other@example.edu")
    again = _registration(event, "Other College", "student@example.com")

    response = client.get(reverse("registration_review_list"))

    rows = dict(response.context["registrations"])
    assert rows[given] is None
    assert rows[again].held_by == imported
    assert "Already held by Team 07" in response.content.decode()


def test_approve_is_refused_while_another_registration_of_the_school_is_approved(client, event):
    _registration(event, "Example University", "student@example.com", status="approved")
    real = _registration(event, "example university", "captain@example.edu")

    response = client.post(reverse("registration_approve", args=[real.pk]), follow=True)

    real.refresh_from_db()
    assert real.status == "pending"
    assert real.approved_by is None
    assert (
        "Registration for example university was not approved: another registration for that school, from "
        "student@example.com, is already approved for one of its events."
    ) in response.content.decode()


@pytest.mark.parametrize("other", ["pending", "approved an earlier season"])
def test_approve_takes_when_no_other_registration_of_the_school_is_approved_for_its_events(client, event, other):
    if other == "pending":
        _registration(event, "Example University", "student@example.com")
    else:
        season = Season.objects.create(name="2025", year=2025)
        earlier = Event.objects.create(
            season=season, name="Invitational", event_type="invitational", date=date(2025, 10, 4), is_finalized=True
        )
        _registration(earlier, "Example University", "captain@example.edu", status="approved")
    registration = _registration(event, "Example University", "captain@example.edu")

    client.post(reverse("registration_approve", args=[registration.pk]))

    registration.refresh_from_db()
    assert registration.status == "approved"


def test_approve_takes_the_lock_before_checking_for_another_approved_registration(client, event):
    """Two reviewers approving two registrations of one school at once: the second waits, then sees the first."""
    registration = _registration(event, "Example University", "captain@example.edu")

    with CaptureQueriesContext(connection) as queries:
        client.post(reverse("registration_approve", args=[registration.pk]))

    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, q in enumerate(sql) if "pg_advisory_xact_lock" in q)
    check = next(i for i, q in enumerate(sql) if "'approved'" in q and "registration_teamregistration" in q)
    assert lock < check


def test_an_approved_registration_is_rejected_only_as_one_of_two_of_its_school(client, event):
    impostor = _registration(event, "Example University", "student@example.com", status="approved")
    real = _registration(event, "Example University", "captain@example.edu", status="approved")

    review = client.get(reverse("registration_review_list")).content.decode()
    assert review.count(reverse("registration_reject", args=[impostor.pk])) == 1
    assert review.count(reverse("registration_reject", args=[real.pk])) == 1

    client.post(reverse("registration_reject", args=[impostor.pk]), {"reason": "Not the school"})
    response = client.post(reverse("registration_reject", args=[real.pk]), {"reason": "Oops"}, follow=True)

    impostor.refresh_from_db()
    real.refresh_from_db()
    assert (impostor.status, real.status) == ("rejected", "approved")
    assert "can be rejected only while another approved one names its school" in response.content.decode()
    assert (
        reverse("registration_reject", args=[real.pk])
        not in client.get(reverse("registration_review_list")).content.decode()
    )


def test_an_approved_registration_with_a_team_is_not_rejected(client, event):
    assigned = _registration(event, "Example University", "captain@example.edu", status="approved")
    set_team_school(
        Team.objects.create(team_number=4, team_name="Team 04"),
        "Example University",
        contact_email="captain@example.edu",
        registration=assigned,
    )
    _registration(event, "Example University", "student@example.com", status="approved")

    client.post(reverse("registration_reject", args=[assigned.pk]), {"reason": "Not the school"})

    assigned.refresh_from_db()
    assert assigned.status == "approved"
