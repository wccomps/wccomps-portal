"""Tests for registration views."""

from datetime import timedelta

import pytest
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from core.tests.roles import role_user

from ..models import Event, RegistrationContact, Season, TeamRegistration
from ..views import RegistrationPageStatus, registration_page_status


class RegistrationViewTestCase(TestCase):
    """Test public registration view."""

    def setUp(self):
        """Set up test client and create an active season with event."""
        self.client = Client()
        self.season = Season.objects.create(name="2026 Season", year=2026, is_active=True)
        self.event = Event.objects.create(
            season=self.season,
            name="Invitational 1",
            event_type="invitational",
            event_number=1,
            date="2026-01-15",
            registration_open=True,
        )

    def test_registration_page_loads(self):
        """Test registration page loads without authentication."""
        response = self.client.get(reverse("registration_register"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/register.html")

    def test_registration_page_has_form(self):
        """Test registration page contains form."""
        response = self.client.get(reverse("registration_register"))
        self.assertContains(response, "school_name")
        self.assertContains(response, "captain_name")
        self.assertContains(response, "captain_email")
        self.assertContains(response, "coach_name")
        self.assertContains(response, "agree_to_rules")

    def test_submit_valid_registration(self):
        """Test submitting valid registration."""
        form_data = {
            "school_name": "Test High School",
            "region": "wrccdc",
            "captain_name": "John Doe",
            "captain_email": "captain@example.com",
            "captain_phone": "555-1234",
            "coach_name": "Dr. Smith",
            "coach_email": "coach@example.com",
            "coach_phone": "",
            "events": [self.event.id],
            "agree_to_rules": True,
        }
        response = self.client.post(reverse("registration_register"), data=form_data)
        self.assertEqual(response.status_code, 302)

        registration = TeamRegistration.objects.get(school_name="Test High School")
        self.assertEqual(registration.status, "pending")
        self.assertEqual(registration.region, "wrccdc")

        captain = registration.contacts.get(role="captain")
        self.assertEqual(captain.name, "John Doe")
        self.assertEqual(captain.email, "captain@example.com")

        coach = registration.contacts.get(role="coach")
        self.assertEqual(coach.name, "Dr. Smith")

    def test_submit_invalid_registration(self):
        """Test submitting invalid registration shows errors."""
        form_data = {
            "school_name": "",
            "captain_name": "",
            "captain_email": "invalid-email",
            "captain_phone": "",
        }
        response = self.client.post(reverse("registration_register"), data=form_data)
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "school_name", "This field is required.")

    def test_form_level_error_is_shown(self):
        """A non-WRCCDC school choosing a Qualifier gets the form's own error, which belongs to no field."""
        qualifier = Event.objects.create(
            season=self.season,
            name="Qualifier",
            event_type="qualifier",
            event_number=2,
            date="2026-02-15",
            registration_open=True,
        )
        form_data = {
            "school_name": "Test High School",
            "region": "mwccdc",
            "captain_name": "John Doe",
            "captain_email": "captain@example.com",
            "captain_phone": "555-1234",
            "coach_name": "Dr. Smith",
            "coach_email": "coach@example.com",
            "events": [qualifier.id],
            "agree_to_rules": True,
        }
        response = self.client.post(reverse("registration_register"), data=form_data)
        self.assertContains(response, "Please remove: Qualifier")

    def test_rate_limit_after_10_successful_submissions(self):
        """Test that 11th registration from same IP gets 429."""
        url = reverse("registration_register")
        for i in range(10):
            form_data = {
                "school_name": f"School {i}",
                "region": "wrccdc",
                "captain_name": "John Doe",
                "captain_email": f"captain{i}@example.com",
                "captain_phone": "555-1234",
                "coach_name": "Dr. Smith",
                "coach_email": f"coach{i}@example.com",
                "coach_phone": "",
                "events": [self.event.id],
                "agree_to_rules": True,
            }
            response = self.client.post(url, data=form_data)
            self.assertEqual(response.status_code, 302, f"Submission {i + 1} should succeed")

        self.assertEqual(TeamRegistration.objects.count(), 10)

        # 11th submission should be rate limited
        form_data = {
            "school_name": "School 10",
            "region": "wrccdc",
            "captain_name": "John Doe",
            "captain_email": "captain10@example.com",
            "captain_phone": "555-1234",
            "coach_name": "Dr. Smith",
            "coach_email": "coach10@example.com",
            "coach_phone": "",
            "events": [self.event.id],
            "agree_to_rules": True,
        }
        response = self.client.post(url, data=form_data)
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many registration submissions", status_code=429)
        self.assertEqual(TeamRegistration.objects.count(), 10)


class AdminReviewListViewTestCase(TestCase):
    """Test admin review list view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.gold_user = role_user("gold_team")
        self.registration = TeamRegistration.objects.create(school_name="Test School")
        RegistrationContact.objects.create(
            registration=self.registration,
            role="captain",
            name="John Doe",
            email="test@example.com",
            phone="555-1234",
        )

    def test_review_list_requires_gold_team(self):
        """Test review list requires Gold Team permission."""
        response = self.client.get(reverse("registration_review_list"))
        self.assertEqual(response.status_code, 302)

    def test_review_list_loads_for_gold_team(self):
        """Test review list loads for Gold Team users."""
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_review_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/review_list.html")

    def test_review_list_shows_registrations(self):
        """Test review list displays all registrations."""
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_review_list"))
        self.assertContains(response, "Test School")

    def test_review_list_shows_captain_contact(self):
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_review_list"))
        self.assertContains(response, "test@example.com")
        self.assertContains(response, "555-1234")

    def test_review_list_filters_by_status(self):
        """Test review list can filter by status."""
        approved_reg = TeamRegistration.objects.create(school_name="Approved School", status="approved")
        RegistrationContact.objects.create(
            registration=approved_reg,
            role="captain",
            name="Jane Doe",
            email="approved@example.com",
        )
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_review_list"), {"status": "pending"})
        self.assertContains(response, "Test School")
        self.assertNotContains(response, "Approved School")


class AdminApproveViewTestCase(TestCase):
    """Test admin approve action view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.gold_user = role_user("gold_team")
        self.registration = TeamRegistration.objects.create(school_name="Test School")
        RegistrationContact.objects.create(
            registration=self.registration,
            role="captain",
            name="John Doe",
            email="test@example.com",
            phone="555-1234",
        )

    def test_approve_requires_gold_team(self):
        """Test approve action requires Gold Team permission."""
        response = self.client.post(reverse("registration_approve", args=[self.registration.id]))
        self.assertEqual(response.status_code, 302)

    def test_approve_registration(self):
        """Test approving a registration."""
        self.client.force_login(self.gold_user)
        response = self.client.post(reverse("registration_approve", args=[self.registration.id]))
        self.assertEqual(response.status_code, 302)

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.status, "approved")
        self.assertIsNotNone(self.registration.approved_at)
        self.assertEqual(self.registration.approved_by, self.gold_user)


class AdminRejectViewTestCase(TestCase):
    """Test admin reject action view."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.gold_user = role_user("gold_team")
        self.registration = TeamRegistration.objects.create(school_name="Test School")
        RegistrationContact.objects.create(
            registration=self.registration,
            role="captain",
            name="John Doe",
            email="test@example.com",
            phone="555-1234",
        )

    def test_reject_requires_gold_team(self):
        """Test reject action requires Gold Team permission."""
        response = self.client.post(reverse("registration_reject", args=[self.registration.id]))
        self.assertEqual(response.status_code, 302)

    def test_reject_registration(self):
        """Test rejecting a registration."""
        self.client.force_login(self.gold_user)
        response = self.client.post(
            reverse("registration_reject", args=[self.registration.id]), {"reason": "Incomplete information"}
        )
        self.assertEqual(response.status_code, 302)

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.status, "rejected")
        self.assertEqual(self.registration.rejection_reason, "Incomplete information")

    def test_reject_page_shows_the_registration(self):
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_reject", args=[self.registration.id]))
        self.assertContains(response, "Test School")
        self.assertContains(response, "test@example.com")

    def test_reject_requires_reason(self):
        """Test reject action requires a reason."""
        self.client.force_login(self.gold_user)
        response = self.client.post(reverse("registration_reject", args=[self.registration.id]), {"reason": ""})
        self.assertEqual(response.status_code, 200)


class SeasonViewTestCase(TestCase):
    """Test season management views."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.gold_user = role_user("gold_team")

    def test_season_list_requires_gold_team(self):
        """Test season list requires Gold Team permission."""
        response = self.client.get(reverse("registration_season_list"))
        self.assertEqual(response.status_code, 302)

    def test_season_list_loads(self):
        """Test season list loads for Gold Team users."""
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_season_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/seasons/list.html")

    def test_season_form_has_one_heading(self):
        """The view passes title, which Django's admin base prints as an h1 of its own on Django admin's pages."""
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_season_create"))
        self.assertEqual(response.content.decode().count("<h1"), 1)

    def test_season_create(self):
        """Test creating a season."""
        from ..models import Season

        self.client.force_login(self.gold_user)
        response = self.client.post(
            reverse("registration_season_create"),
            data={"name": "2026 Season", "year": 2026, "is_active": True},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Season.objects.filter(year=2026).exists())


class EventViewTestCase(TestCase):
    """Test event management views."""

    def setUp(self):
        """Set up test data."""

        from ..models import Season

        self.client = Client()
        self.gold_user = role_user("gold_team")
        self.season = Season.objects.create(name="2026 Season", year=2026, is_active=True)

    def test_event_list_requires_gold_team(self):
        """Test event list requires Gold Team permission."""
        response = self.client.get(reverse("registration_event_list", args=[self.season.id]))
        self.assertEqual(response.status_code, 302)

    def _badges_for(self, season_active, **event_fields):
        from ..models import Event, Season

        season = Season.objects.create(name="Other Season", year=2027)
        if season_active:
            season.activate()
        event = Event.objects.create(
            season=season,
            name="Invitational 9",
            event_type="invitational",
            event_number=9,
            date="2027-01-15",
            **event_fields,
        )
        self.client.force_login(self.gold_user)
        listed = self.client.get(reverse("registration_event_list", args=[season.id])).content.decode()
        detail = self.client.get(reverse("registration_event_detail", args=[event.id])).content.decode()
        return listed, detail

    def test_an_event_in_an_inactive_season_shows_registration_closed(self):
        """The public form never offers it, so its pages don't call it open."""
        listed, detail = self._badges_for(season_active=False)
        self.assertRegex(listed, r">\s*Closed\s*<")
        self.assertNotRegex(listed, r">\s*Open\s*<")
        self.assertIn("Registration Closed", detail)
        self.assertNotIn("Registration Open", detail)

    def test_an_event_past_its_deadline_shows_registration_closed(self):
        listed, detail = self._badges_for(season_active=True, registration_deadline=timezone.now() - timedelta(hours=1))
        self.assertRegex(listed, r">\s*Closed\s*<")
        self.assertIn("Registration Closed", detail)

    def test_an_open_event_in_the_active_season_shows_registration_open(self):
        listed, detail = self._badges_for(season_active=True)
        self.assertRegex(listed, r">\s*Open\s*<")
        self.assertIn("Registration Open", detail)

    def test_event_list_loads(self):
        """Test event list loads for Gold Team users."""
        self.client.force_login(self.gold_user)
        response = self.client.get(reverse("registration_event_list", args=[self.season.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/events/list.html")

    def test_event_create(self):
        """Test creating an event."""
        from ..models import Event

        self.client.force_login(self.gold_user)
        response = self.client.post(
            reverse("registration_event_create", args=[self.season.id]),
            data={
                "name": "Invitational #1",
                "event_type": "invitational",
                "event_number": 1,
                "date": "2026-01-15",
                "start_time": "09:00",
                "end_time": "17:00",
                "max_teams": 50,
                "registration_open": True,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Event.objects.filter(name="Invitational #1").exists())


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("finalized", "active", "registration_open", "status"),
    [
        (True, True, True, RegistrationPageStatus.FINALIZED),
        (True, False, False, RegistrationPageStatus.FINALIZED),
        (False, True, True, RegistrationPageStatus.ACTIVE),
        (False, True, False, RegistrationPageStatus.ACTIVE),
        (False, False, True, RegistrationPageStatus.REGISTRATION_OPEN),
        (False, False, False, RegistrationPageStatus.REGISTRATION_CLOSED),
    ],
)
def test_event_status_takes_the_first_that_holds(finalized, active, registration_open, status):
    """Finalized, then active, then open or closed for registration."""
    season = Season.objects.create(name="2026 Season", year=2026)
    season.activate()
    saved = Event.objects.create(
        season=season,
        name="Invitational 1",
        event_type="invitational",
        event_number=1,
        date="2026-01-15",
        registration_open=registration_open,
        is_finalized=finalized,
        is_active=active,
    )
    assert registration_page_status(Event.objects.with_registration_open().get(pk=saved.pk)) is status
