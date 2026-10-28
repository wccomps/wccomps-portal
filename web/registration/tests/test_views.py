"""Tests for registration views."""

from django.test import Client, TestCase
from django.urls import reverse

from core.tests.roles import role_user

from ..models import Event, RegistrationContact, RegistrationEventEnrollment, Season, TeamRegistration


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


class TokenEditViewTestCase(TestCase):
    """Test token-based registration editing."""

    def setUp(self):
        """Set up test data."""
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
        self.registration = TeamRegistration.objects.create(school_name="Test School")
        RegistrationContact.objects.create(
            registration=self.registration,
            role="captain",
            name="John Doe",
            email="john@example.com",
            phone="555-1234",
        )
        RegistrationContact.objects.create(
            registration=self.registration,
            role="coach",
            name="Dr. Coach",
            email="coach@example.com",
            phone="555-9999",
        )

    def test_edit_page_loads_with_valid_token(self):
        """Test edit page loads with valid token."""
        response = self.client.get(reverse("registration_edit", args=[self.registration.edit_token]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/edit.html")

    def test_edit_page_renders_every_form_field(self):
        """Every field the form needs is an input on the page, filled in from the registration."""
        response = self.client.get(reverse("registration_edit", args=[self.registration.edit_token]))
        for name in response.context["form"].fields:
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'value="john@example.com"')
        self.assertContains(response, 'value="coach@example.com"')

    def test_edit_page_shows_form_level_error(self):
        """A non-WRCCDC school choosing a Qualifier gets the form's own error on the edit page too."""
        qualifier = Event.objects.create(
            season=self.season,
            name="Qualifier",
            event_type="qualifier",
            event_number=2,
            date="2026-02-15",
            registration_open=True,
        )
        form_data = {
            "school_name": "Test School",
            "region": "mwccdc",
            "captain_name": "John Doe",
            "captain_email": "john@example.com",
            "captain_phone": "555-1234",
            "coach_name": "Dr. Coach",
            "coach_email": "coach@example.com",
            "events": [qualifier.id],
            "agree_to_rules": True,
        }
        response = self.client.post(reverse("registration_edit", args=[self.registration.edit_token]), data=form_data)
        self.assertContains(response, "Please remove: Qualifier")

    def _contact_data(self, **extra):
        return {
            "school_name": "Test School",
            "region": "wrccdc",
            "captain_name": "John Doe",
            "captain_email": "john@example.com",
            "captain_phone": "555-0000",
            "coach_name": "Dr. Coach",
            "coach_email": "coach@example.com",
            "agree_to_rules": True,
            **extra,
        }

    def _closed_event_enrollment(self):
        closed = Event.objects.create(
            season=self.season,
            name="Closed Invitational",
            event_type="invitational",
            event_number=3,
            date="2026-01-01",
            registration_open=False,
        )
        RegistrationEventEnrollment.objects.create(registration=self.registration, event=closed)
        return closed

    def test_edit_keeps_enrollments_in_events_whose_registration_closed(self):
        """The form offers only open events: saving it can't drop an enrollment it never showed."""
        closed = self._closed_event_enrollment()
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]),
            data=self._contact_data(events=[self.event.id]),
        )
        self.assertEqual(response.status_code, 302)
        enrolled = set(self.registration.event_enrollments.values_list("event_id", flat=True))
        self.assertEqual(enrolled, {closed.id, self.event.id})

    def test_edit_saves_with_no_events_open(self):
        """Once registration closes everywhere, a coach can still correct a phone number."""
        self.event.registration_open = False
        self.event.save()
        RegistrationEventEnrollment.objects.create(registration=self.registration, event=self.event)
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]), data=self._contact_data()
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.registration.contacts.get(role="captain").phone, "555-0000")
        self.assertTrue(self.registration.event_enrollments.filter(event=self.event).exists())

    def test_edit_may_leave_open_events_unticked_when_enrolled_elsewhere(self):
        """An enrollment in a closed event is a registration for something: no open event has to be ticked."""
        self._closed_event_enrollment()
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]), data=self._contact_data()
        )
        self.assertEqual(response.status_code, 302)

    def _closed_qualifier_enrollment(self):
        qualifier = Event.objects.create(
            season=self.season,
            name="Closed Qualifier",
            event_type="qualifier",
            event_number=4,
            date="2026-02-01",
            registration_open=False,
        )
        RegistrationEventEnrollment.objects.create(registration=self.registration, event=qualifier)
        return qualifier

    def test_edit_checks_the_region_against_enrollments_it_keeps(self):
        """A team in a Qualifier whose registration closed can't save itself into a region that may not enter it."""
        self._closed_qualifier_enrollment()
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]),
            data=self._contact_data(region="mwccdc", events=[self.event.id], shown_events=[self.event.id]),
        )
        self.assertContains(response, "Closed Qualifier")
        self.registration.refresh_from_db()
        self.assertEqual(self.registration.region, "wrccdc")

    def test_edit_shows_why_a_ticked_event_that_closed_meanwhile_is_refused(self):
        """The page was loaded while the event was open; by the save its registration had closed."""
        self.event.registration_open = False
        self.event.save()
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]),
            data=self._contact_data(events=[self.event.id], shown_events=[self.event.id]),
        )
        self.assertContains(response, "Select a valid choice")

    def test_edit_keeps_an_enrollment_in_an_event_that_reopened_meanwhile(self):
        """Only the events the page showed can be unticked: one that opened after the page loaded keeps its team."""
        closed = self._closed_event_enrollment()
        closed.registration_open = True
        closed.save()
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]),
            data=self._contact_data(events=[self.event.id], shown_events=[self.event.id]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.registration.event_enrollments.filter(event=closed).exists())

    def test_edit_unticking_a_shown_event_drops_it(self):
        other = Event.objects.create(
            season=self.season,
            name="Invitational 2",
            event_type="invitational",
            event_number=5,
            date="2026-03-01",
            registration_open=True,
        )
        RegistrationEventEnrollment.objects.create(registration=self.registration, event=other)
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]),
            data=self._contact_data(events=[self.event.id], shown_events=[self.event.id, other.id]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.registration.event_enrollments.filter(event=other).exists())

    def test_edit_page_lists_enrollments_in_closed_events(self):
        """The checkboxes offer only open events, so the page says what else the team is registered for."""
        self._closed_event_enrollment()
        response = self.client.get(reverse("registration_edit", args=[self.registration.edit_token]))
        self.assertContains(response, "Closed Invitational")

    def test_edit_page_404_with_invalid_token(self):
        """Test edit page returns 404 with invalid token."""
        response = self.client.get(reverse("registration_edit", args=["invalid-token"]))
        self.assertEqual(response.status_code, 404)

    def test_edit_locked_after_approval(self):
        """Test edit page shows locked message after approval."""
        self.registration.status = "approved"
        self.registration.save()
        response = self.client.get(reverse("registration_edit", args=[self.registration.edit_token]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/edit_locked.html")

    def test_edit_updates_registration(self):
        """Test edit form updates registration."""
        form_data = {
            "school_name": "Updated School",
            "region": "mwccdc",
            "captain_name": "Jane Doe",
            "captain_email": "jane@example.com",
            "captain_phone": "555-5678",
            "coach_name": "Dr. Updated",
            "coach_email": "updated@example.com",
            "coach_phone": "",
            "events": [self.event.id],
            "agree_to_rules": True,
        }
        response = self.client.post(
            reverse("registration_edit", args=[self.registration.edit_token]),
            data=form_data,
        )
        self.assertEqual(response.status_code, 302)

        self.registration.refresh_from_db()
        self.assertEqual(self.registration.school_name, "Updated School")
        self.assertEqual(self.registration.region, "mwccdc")

        captain = self.registration.contacts.get(role="captain")
        self.assertEqual(captain.name, "Jane Doe")
        self.assertEqual(captain.email, "jane@example.com")


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
