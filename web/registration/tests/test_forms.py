"""Tests for registration forms."""

import pytest

from ..forms import (
    RegistrationForm,
)
from ..models import Event, RegistrationContact, RegistrationEventEnrollment, Season, TeamRegistration

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


@pytest.fixture
def form_data():
    """A filled-in registration form with no events ticked."""
    return {
        "school_name": "Test High School",
        "region": "wrccdc",
        "captain_name": "John Doe",
        "captain_email": "captain@example.com",
        "captain_phone": "555-1234",
        "coach_name": "Dr. Smith",
        "coach_email": "coach@example.com",
        "coach_phone": "555-5678",
        "agree_to_rules": True,
    }


@pytest.fixture
def valid_form_data(form_data, active_season_with_event):
    """The form with the open event ticked."""
    _, event = active_season_with_event
    return {**form_data, "events": [event.id]}


class TestRegistrationFormValidation:
    """Test RegistrationForm validation behavior."""

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


class TestRegistrationFormAtomicSave:
    """A registration is saved whole or not at all."""

    @pytest.mark.parametrize("failing", ["contact", "enrollment"])
    def test_a_failed_write_leaves_no_registration(self, valid_form_data, monkeypatch, failing):
        def fail(*args, **kwargs):
            raise RuntimeError("write failed")

        if failing == "contact":
            monkeypatch.setattr(RegistrationContact.objects, "create", fail)
        else:
            monkeypatch.setattr(RegistrationEventEnrollment.objects, "bulk_create", fail)
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        with pytest.raises(RuntimeError):
            form.save()
        assert not TeamRegistration.objects.exists()

    def test_saving_without_commit_is_refused(self, valid_form_data):
        """A registration without its contacts and enrollments is never handed out."""
        form = RegistrationForm(data=valid_form_data)
        assert form.is_valid(), form.errors
        with pytest.raises(ValueError, match="commit"):
            form.save(commit=False)
        assert not TeamRegistration.objects.exists()


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

    def test_wrccdc_can_register_for_qualifier(self, form_data, season_with_events):
        """WRCCDC teams can register for qualifiers."""
        _, _, qualifier, _ = season_with_events
        form_data["region"] = "wrccdc"
        form_data["events"] = [qualifier.id]
        form = RegistrationForm(data=form_data)
        assert form.is_valid(), form.errors

    def test_wrccdc_can_register_for_regional(self, form_data, season_with_events):
        """WRCCDC teams can register for regionals."""
        _, _, _, regional = season_with_events
        form_data["region"] = "wrccdc"
        form_data["events"] = [regional.id]
        form = RegistrationForm(data=form_data)
        assert form.is_valid(), form.errors

    def test_non_wrccdc_can_register_for_invitational(self, form_data, season_with_events):
        """Non-WRCCDC teams can register for invitationals."""
        _, invitational, _, _ = season_with_events
        form_data["region"] = "mwccdc"
        form_data["events"] = [invitational.id]
        form = RegistrationForm(data=form_data)
        assert form.is_valid(), form.errors

    def test_non_wrccdc_blocked_from_qualifier(self, form_data, season_with_events):
        """Non-WRCCDC teams cannot register for qualifiers."""
        _, _, qualifier, _ = season_with_events
        form_data["region"] = "mwccdc"
        form_data["events"] = [qualifier.id]
        form = RegistrationForm(data=form_data)
        assert not form.is_valid()
        assert "__all__" in form.errors
        assert "Only Western Regional (WRCCDC) teams" in str(form.errors["__all__"])

    def test_non_wrccdc_blocked_from_regional(self, form_data, season_with_events):
        """Non-WRCCDC teams cannot register for regionals."""
        _, _, _, regional = season_with_events
        form_data["region"] = "neccdc"
        form_data["events"] = [regional.id]
        form = RegistrationForm(data=form_data)
        assert not form.is_valid()
        assert "__all__" in form.errors
        assert "Only Western Regional (WRCCDC) teams" in str(form.errors["__all__"])

    def test_non_wrccdc_blocked_mixed_events(self, form_data, season_with_events):
        """Non-WRCCDC teams cannot register for mix of invitational and qualifier."""
        _, invitational, qualifier, _ = season_with_events
        form_data["region"] = "seccdc"
        form_data["events"] = [invitational.id, qualifier.id]
        form = RegistrationForm(data=form_data)
        assert not form.is_valid()
        assert "__all__" in form.errors
        assert "Qualifier 1" in str(form.errors["__all__"])

    def test_at_large_blocked_from_qualifier(self, form_data, season_with_events):
        """At-large teams cannot register for qualifiers."""
        _, _, qualifier, _ = season_with_events
        form_data["region"] = "at_large"
        form_data["events"] = [qualifier.id]
        form = RegistrationForm(data=form_data)
        assert not form.is_valid()
        assert "__all__" in form.errors
