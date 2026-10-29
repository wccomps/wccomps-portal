"""Tests for registration forms."""

import pytest

from ..forms import (
    RegistrationForm,
)
from ..models import Event, Season

pytestmark = pytest.mark.django_db


@pytest.fixture
def active_season_with_event(db):
    """Create an active season with an open event."""
    season = Season.objects.create(name="2026 Season", year=2026, is_active=True)
    event = Event.objects.create(
        season=season,
        name="Invitational 1",
        event_type="invitational",
        event_number=1,
        date="2026-01-15",
        registration_open=True,
    )
    return season, event


class TestRegistrationFormValidation:
    """Test RegistrationForm validation behavior."""

    @pytest.fixture
    def valid_form_data(self, active_season_with_event):
        """Valid form data for testing."""
        _, event = active_season_with_event
        return {
            "school_name": "Test High School",
            "region": "wrccdc",
            "captain_name": "John Doe",
            "captain_email": "captain@example.com",
            "captain_phone": "555-1234",
            "coach_name": "Dr. Smith",
            "coach_email": "coach@example.com",
            "coach_phone": "",
            "events": [event.id],
            "agree_to_rules": True,
        }

    def test_form_accepts_valid_data(self, valid_form_data):
        """Form should accept valid data."""
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors

    @pytest.mark.parametrize(
        "missing_field",
        ["school_name", "captain_name", "captain_email", "captain_phone", "coach_name", "coach_email"],
    )
    def test_form_rejects_missing_required_field(self, valid_form_data, missing_field):
        """Form should reject data missing required fields."""
        del valid_form_data[missing_field]
        form = RegistrationForm(data=valid_form_data)
        assert not form.is_valid()
        assert missing_field in form.errors

    def test_form_rejects_invalid_captain_email(self, valid_form_data):
        """Form should reject invalid email format."""
        valid_form_data["captain_email"] = "not-an-email"
        form = RegistrationForm(data=valid_form_data)
        assert not form.is_valid()
        assert "captain_email" in form.errors

    def test_form_rejects_missing_events(self, valid_form_data):
        """Form should reject when no events selected."""
        valid_form_data["events"] = []
        form = RegistrationForm(data=valid_form_data)
        assert not form.is_valid()
        assert "events" in form.errors

    def test_form_rejects_missing_rules_agreement(self, valid_form_data):
        """Form should reject when rules not agreed to."""
        valid_form_data["agree_to_rules"] = False
        form = RegistrationForm(data=valid_form_data)
        assert not form.is_valid()
        assert "agree_to_rules" in form.errors


class TestRegistrationFormSave:
    """Test RegistrationForm save behavior."""

    @pytest.fixture
    def valid_form_data(self, active_season_with_event):
        """Valid form data for testing."""
        _, event = active_season_with_event
        return {
            "school_name": "Test High School",
            "region": "wrccdc",
            "captain_name": "John Doe",
            "captain_email": "captain@example.com",
            "captain_phone": "555-1234",
            "coach_name": "Dr. Smith",
            "coach_email": "coach@example.com",
            "coach_phone": "555-5678",
            "events": [event.id],
            "agree_to_rules": True,
        }

    def test_form_creates_registration_with_pending_status(self, valid_form_data):
        """Saved form should create registration with pending status."""
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        registration = form.save()
        assert registration.pk is not None
        assert registration.status == "pending"

    def test_form_creates_captain_contact(self, valid_form_data):
        """Saved form should create captain contact."""
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        registration = form.save()

        captain = registration.contacts.get(role="captain")
        assert captain.name == "John Doe"
        assert captain.email == "captain@example.com"
        assert captain.phone == "555-1234"

    def test_form_creates_coach_contact(self, valid_form_data):
        """Saved form should create coach contact."""
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        registration = form.save()

        coach = registration.contacts.get(role="coach")
        assert coach.name == "Dr. Smith"
        assert coach.email == "coach@example.com"
        assert coach.phone == "555-5678"

    def test_form_creates_event_enrollments(self, valid_form_data, active_season_with_event):
        """Saved form should create event enrollments."""
        _, event = active_season_with_event
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        registration = form.save()

        enrollments = registration.event_enrollments.all()
        assert enrollments.count() == 1
        assert enrollments.first().event == event

    def test_form_edit_removes_unchecked_event_enrollments(self, valid_form_data, active_season_with_event):
        """Editing an existing registration removes enrollments for unchecked events."""
        season, event1 = active_season_with_event
        event2 = Event.objects.create(
            season=season,
            name="Invitational 2",
            event_type="invitational",
            event_number=2,
            date="2026-02-15",
            registration_open=True,
        )
        valid_form_data["events"] = [event1.id, event2.id]
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        registration = form.save()
        assert set(registration.event_enrollments.values_list("event_id", flat=True)) == {event1.id, event2.id}

        valid_form_data["events"] = [event2.id]
        valid_form_data["shown_events"] = [event1.id, event2.id]
        valid_form_data["events_listed"] = "1"
        edit_form = RegistrationForm(data=valid_form_data, instance=registration)
        assert edit_form.is_valid(), edit_form.errors
        edit_form.save()
        assert list(registration.event_enrollments.values_list("event_id", flat=True)) == [event2.id]

    def test_registration_has_edit_token(self, valid_form_data):
        """Saved registration should have an edit token."""
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        registration = form.save()
        assert registration.edit_token
        assert len(registration.edit_token) > 32


class TestRegistrationFormRegionValidation:
    """Test RegistrationForm region validation behavior."""

    @pytest.fixture
    def season_with_events(self, db):
        """Create a season with invitational, qualifier, and regional events."""
        from datetime import date

        season = Season.objects.create(name="2026 Season", year=2026, is_active=True)
        invitational = Event.objects.create(
            season=season,
            name="Invitational 1",
            event_type="invitational",
            event_number=1,
            date=date(2026, 1, 15),
            registration_open=True,
        )
        qualifier = Event.objects.create(
            season=season,
            name="Qualifier 1",
            event_type="qualifier",
            event_number=1,
            date=date(2026, 2, 15),
            registration_open=True,
        )
        regional = Event.objects.create(
            season=season,
            name="Regional",
            event_type="regional",
            event_number=1,
            date=date(2026, 3, 15),
            registration_open=True,
        )
        return season, invitational, qualifier, regional

    @pytest.fixture
    def base_form_data(self):
        """Base form data without events."""
        return {
            "school_name": "Test High School",
            "captain_name": "John Doe",
            "captain_email": "captain@example.com",
            "captain_phone": "555-1234",
            "coach_name": "Dr. Smith",
            "coach_email": "coach@example.com",
            "coach_phone": "",
            "agree_to_rules": True,
        }

    def test_wrccdc_can_register_for_qualifier(self, base_form_data, season_with_events):
        """WRCCDC teams can register for qualifiers."""
        _, _, qualifier, _ = season_with_events
        base_form_data["region"] = "wrccdc"
        base_form_data["events"] = [qualifier.id]
        form = RegistrationForm(data=base_form_data)
        assert form.is_valid(), form.errors

    def test_wrccdc_can_register_for_regional(self, base_form_data, season_with_events):
        """WRCCDC teams can register for regionals."""
        _, _, _, regional = season_with_events
        base_form_data["region"] = "wrccdc"
        base_form_data["events"] = [regional.id]
        form = RegistrationForm(data=base_form_data)
        assert form.is_valid(), form.errors

    def test_non_wrccdc_can_register_for_invitational(self, base_form_data, season_with_events):
        """Non-WRCCDC teams can register for invitationals."""
        _, invitational, _, _ = season_with_events
        base_form_data["region"] = "mwccdc"
        base_form_data["events"] = [invitational.id]
        form = RegistrationForm(data=base_form_data)
        assert form.is_valid(), form.errors

    def test_non_wrccdc_blocked_from_qualifier(self, base_form_data, season_with_events):
        """Non-WRCCDC teams cannot register for qualifiers."""
        _, _, qualifier, _ = season_with_events
        base_form_data["region"] = "mwccdc"
        base_form_data["events"] = [qualifier.id]
        form = RegistrationForm(data=base_form_data)
        assert not form.is_valid()
        assert "events" in form.errors
        assert "Only Western Regional (WRCCDC) teams" in str(form.errors["events"])

    def test_non_wrccdc_blocked_from_regional(self, base_form_data, season_with_events):
        """Non-WRCCDC teams cannot register for regionals."""
        _, _, _, regional = season_with_events
        base_form_data["region"] = "neccdc"
        base_form_data["events"] = [regional.id]
        form = RegistrationForm(data=base_form_data)
        assert not form.is_valid()
        assert "events" in form.errors
        assert "Only Western Regional (WRCCDC) teams" in str(form.errors["events"])

    def test_non_wrccdc_blocked_mixed_events(self, base_form_data, season_with_events):
        """Non-WRCCDC teams cannot register for mix of invitational and qualifier."""
        _, invitational, qualifier, _ = season_with_events
        base_form_data["region"] = "seccdc"
        base_form_data["events"] = [invitational.id, qualifier.id]
        form = RegistrationForm(data=base_form_data)
        assert not form.is_valid()
        assert "events" in form.errors
        assert "Qualifier 1" in str(form.errors["events"])

    def test_at_large_blocked_from_qualifier(self, base_form_data, season_with_events):
        """At-large teams cannot register for qualifiers."""
        _, _, qualifier, _ = season_with_events
        base_form_data["region"] = "at_large"
        base_form_data["events"] = [qualifier.id]
        form = RegistrationForm(data=base_form_data)
        assert not form.is_valid()
        assert "events" in form.errors


def test_the_region_rule_names_the_ticked_events_on_the_events_field(active_season_with_event):
    season, _ = active_season_with_event
    qualifier = Event.objects.create(
        season=season, name="Qualifier", event_type="qualifier", date="2026-02-15", registration_open=True
    )
    form = RegistrationForm(data={"region": "mwccdc", "events": [qualifier.pk]})

    assert not form.is_valid()
    assert form.errors["events"] == [
        "Only Western Regional (WRCCDC) teams can register for Qualifiers and Regionals. Please remove: Qualifier"
    ]
    assert "__all__" not in form.errors


def test_a_kept_closed_enrollment_against_the_region_is_the_forms_own_error(active_season_with_event):
    """Unticking can't fix it, so it belongs to no field."""
    from ..models import RegistrationEventEnrollment, TeamRegistration

    season, event = active_season_with_event
    closed = Event.objects.create(
        season=season, name="Closed Qualifier", event_type="qualifier", date="2026-02-01", registration_open=False
    )
    registration = TeamRegistration.objects.create(school_name="Example School", region="wrccdc")
    RegistrationEventEnrollment.objects.create(registration=registration, event=closed)
    form = RegistrationForm(data={"region": "mwccdc", "events": [event.pk]}, instance=registration)

    assert not form.is_valid()
    assert "Closed Qualifier" in str(form.errors["__all__"])
    assert "events" not in form.errors


@pytest.mark.parametrize(
    ("field", "message"),
    [("events", "Select at least one event."), ("agree_to_rules", "Agree to the competition rules to register.")],
)
def test_an_unticked_required_choice_says_what_to_do(active_season_with_event, field, message):
    form = RegistrationForm(data={})

    assert form.errors[field] == [message]


def test_name_placeholders_are_not_sample_names():
    """A placeholder that reads as an entered value hides that the field is empty."""
    form = RegistrationForm()

    for name in ("captain_name", "coach_name"):
        assert "placeholder" not in form.fields[name].widget.attrs
    for name in ("captain_email", "coach_email"):
        assert form.fields[name].widget.attrs["placeholder"] == "name@school.edu"
