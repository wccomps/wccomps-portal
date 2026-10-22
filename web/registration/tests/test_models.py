"""Tests for registration models."""

from datetime import date

import pytest
from django.db import IntegrityError

from ..models import (
    Event,
    EventTeamAssignment,
    RegistrationContact,
    RegistrationEventEnrollment,
    Season,
    TeamRegistration,
)


@pytest.fixture
def registration(db):
    """Create a basic registration for tests."""
    return TeamRegistration.objects.create(school_name="Test High School")


@pytest.fixture
def season(db):
    """Create a season for tests."""
    return Season.objects.create(name="2026 Season", year=2026, is_active=True)


@pytest.fixture
def event(season):
    """Create an event for tests."""
    return Event.objects.create(
        season=season,
        name="Invitational #1",
        event_type="invitational",
        event_number=1,
        date=date(2026, 1, 15),
    )


@pytest.mark.django_db
class TestTeamRegistration:
    """Test TeamRegistration model."""

    def test_registration_creation(self):
        """Test registration can be created with required fields."""
        registration = TeamRegistration.objects.create(school_name="Test High School")
        assert registration.school_name == "Test High School"
        assert registration.status == "pending"
        assert registration.submitted_at is not None
        assert registration.edit_token is not None

    def test_registration_defaults(self):
        """Test registration default values."""
        registration = TeamRegistration.objects.create(school_name="Test School")
        assert registration.status == "pending"
        assert registration.approved_at is None
        assert registration.rejection_reason == ""
        assert registration.approved_by is None

    def test_edit_token_is_unique(self):
        """Test each registration gets a unique edit token."""
        reg1 = TeamRegistration.objects.create(school_name="School 1")
        reg2 = TeamRegistration.objects.create(school_name="School 2")
        assert reg1.edit_token != reg2.edit_token

    def test_status_choices(self):
        """Test valid status choices."""
        valid_statuses = ["pending", "approved", "rejected"]
        for status in valid_statuses:
            registration = TeamRegistration.objects.create(
                school_name=f"School {status}",
                status=status,
            )
            assert registration.status == status

    def test_approve_registration(self, admin_user):
        """Test approving a registration."""
        registration = TeamRegistration.objects.create(school_name="Test School")
        registration.approve(admin_user)
        assert registration.status == "approved"
        assert registration.approved_at is not None
        assert registration.approved_by == admin_user

    def test_reject_registration(self):
        """Test rejecting a registration."""
        registration = TeamRegistration.objects.create(school_name="Test School")
        reason = "Incomplete information"
        registration.reject(reason)
        assert registration.status == "rejected"
        assert registration.rejection_reason == reason

    def test_string_representation(self):
        """Test string representation."""
        registration = TeamRegistration.objects.create(school_name="Test School")
        expected = "Test School (pending)"
        assert str(registration) == expected

    def test_ordering(self):
        """Test registrations are ordered by submission date (newest first)."""
        reg1 = TeamRegistration.objects.create(school_name="School 1")
        reg2 = TeamRegistration.objects.create(school_name="School 2")
        reg3 = TeamRegistration.objects.create(school_name="School 3")

        registrations = list(TeamRegistration.objects.all())
        assert registrations[0] == reg3
        assert registrations[1] == reg2
        assert registrations[2] == reg1


@pytest.mark.django_db
class TestSeason:
    """Test Season model."""

    def test_season_creation(self):
        """Test season can be created."""
        season = Season.objects.create(name="2026 Season", year=2026)
        assert season.name == "2026 Season"
        assert season.year == 2026
        assert season.is_active is False
        assert season.created_at is not None

    def test_season_ordering(self):
        """Test seasons are ordered by year (newest first)."""
        s1 = Season.objects.create(name="2024 Season", year=2024)
        s2 = Season.objects.create(name="2026 Season", year=2026)
        s3 = Season.objects.create(name="2025 Season", year=2025)

        seasons = list(Season.objects.all())
        assert seasons[0] == s2
        assert seasons[1] == s3
        assert seasons[2] == s1

    def test_string_representation(self):
        """Test string representation."""
        season = Season.objects.create(name="2026 Season", year=2026)
        assert str(season) == "2026 Season"


@pytest.mark.django_db
class TestEvent:
    """Test Event model."""

    def test_event_creation(self, season):
        """Test event can be created."""
        event = Event.objects.create(
            season=season,
            name="Invitational #1",
            event_type="invitational",
            event_number=1,
            date=date(2026, 1, 15),
        )
        assert event.name == "Invitational #1"
        assert event.event_type == "invitational"
        assert event.date == date(2026, 1, 15)
        assert event.registration_open is True
        assert event.is_active is False
        assert event.is_finalized is False

    def test_event_defaults(self, season):
        """Test event default values."""
        event = Event.objects.create(
            season=season,
            name="Test Event",
            event_type="invitational",
            date=date(2026, 1, 1),
        )
        # Times stored as strings in default
        assert str(event.start_time)[:5] == "09:00"
        assert str(event.end_time)[:5] == "17:00"
        assert event.max_teams == 50

    def test_string_representation(self, season):
        """Test string representation."""
        event = Event.objects.create(
            season=season,
            name="Invitational #1",
            event_type="invitational",
            date=date(2026, 1, 15),
        )
        assert str(event) == "Invitational #1 (2026 Season)"


@pytest.mark.django_db
class TestRegistrationContact:
    """Test RegistrationContact model."""

    def test_contact_creation(self, registration):
        """Test contact can be created."""
        contact = RegistrationContact.objects.create(
            registration=registration,
            role="captain",
            name="John Doe",
            email="john@example.com",
            phone="555-1234",
        )
        assert contact.name == "John Doe"
        assert contact.role == "captain"
        assert contact.email == "john@example.com"

    def test_contact_roles(self, registration):
        """Test all valid contact roles."""
        roles = ["captain", "co_captain", "coach", "site_judge"]
        for role in roles:
            contact = RegistrationContact.objects.create(
                registration=registration,
                role=role,
                name=f"{role} name",
                email=f"{role}@example.com",
            )
            assert contact.role == role

    def test_unique_role_per_registration(self, registration):
        """Test only one contact per role per registration."""
        RegistrationContact.objects.create(
            registration=registration,
            role="captain",
            name="Captain 1",
            email="captain1@example.com",
        )
        with pytest.raises(IntegrityError):
            RegistrationContact.objects.create(
                registration=registration,
                role="captain",
                name="Captain 2",
                email="captain2@example.com",
            )

    def test_string_representation(self, registration):
        """Test string representation."""
        contact = RegistrationContact.objects.create(
            registration=registration,
            role="captain",
            name="John Doe",
            email="john@example.com",
        )
        assert "John Doe" in str(contact)
        assert "Team Captain" in str(contact)


@pytest.mark.django_db
class TestRegistrationEventEnrollment:
    """Test RegistrationEventEnrollment model."""

    def test_enrollment_creation(self, registration, event):
        """Test enrollment can be created."""
        enrollment = RegistrationEventEnrollment.objects.create(
            registration=registration,
            event=event,
        )
        assert enrollment.registration == registration
        assert enrollment.event == event
        assert enrollment.enrolled_at is not None

    def test_unique_enrollment_per_registration(self, registration, event):
        """Test only one enrollment per registration per event."""
        RegistrationEventEnrollment.objects.create(
            registration=registration,
            event=event,
        )
        with pytest.raises(IntegrityError):
            RegistrationEventEnrollment.objects.create(
                registration=registration,
                event=event,
            )

    def test_string_representation(self, registration, event):
        """Test string representation."""
        enrollment = RegistrationEventEnrollment.objects.create(
            registration=registration,
            event=event,
        )
        assert registration.school_name in str(enrollment)
        assert event.name in str(enrollment)


@pytest.mark.django_db
class TestEventTeamAssignment:
    """Test EventTeamAssignment model."""

    @pytest.fixture
    def team(self, db):
        """Create a team for tests."""
        from team.models import Team

        return Team.objects.create(team_number=1, team_name="Team 01")

    @pytest.fixture
    def school(self, db):
        from registration.models import School

        return School.for_name("Example University")

    def test_assignment_creation(self, registration, school, event, team):
        """Test assignment can be created."""
        assignment = EventTeamAssignment.objects.create(
            event=event,
            school=school,
            registration=registration,
            team=team,
        )
        assert assignment.event == event
        assert assignment.school == school
        assert assignment.registration == registration
        assert assignment.team == team
        assert assignment.assigned_at is not None

    def test_unique_school_per_event(self, school, event, team):
        from team.models import Team

        team2 = Team.objects.create(team_number=2, team_name="Team 02")

        EventTeamAssignment.objects.create(event=event, school=school, team=team)
        with pytest.raises(IntegrityError):
            EventTeamAssignment.objects.create(event=event, school=school, team=team2)

    def test_unique_team_per_event(self, event, team):
        """Test each team can only be assigned once per event."""
        from registration.models import School

        EventTeamAssignment.objects.create(event=event, school=School.for_name("School 1"), team=team)
        with pytest.raises(IntegrityError):
            EventTeamAssignment.objects.create(event=event, school=School.for_name("School 2"), team=team)

    def test_string_representation(self, registration, school, event, team):
        """Test string representation."""
        assignment = EventTeamAssignment.objects.create(
            event=event,
            school=school,
            registration=registration,
            team=team,
        )
        assert school.name in str(assignment)
        assert "Team 01" in str(assignment)


@pytest.mark.django_db
@pytest.mark.parametrize("first", ["approve", "reject"])
@pytest.mark.parametrize("second", ["approve", "reject"])
def test_only_the_first_of_two_reviews_of_a_pending_registration_takes(admin_user, first, second):
    """Two reviewers on the same pending registration (two tabs, a double click): the second sees it decided."""
    registration = TeamRegistration.objects.create(school_name="Test School")
    one, two = TeamRegistration.objects.get(pk=registration.pk), TeamRegistration.objects.get(pk=registration.pk)

    def review(copy, how):
        return copy.approve(admin_user) if how == "approve" else copy.reject("Late")

    assert review(one, first) is True
    decided = TeamRegistration.objects.filter(pk=registration.pk).values().get()
    assert review(two, second) is False
    assert TeamRegistration.objects.filter(pk=registration.pk).values().get() == decided
