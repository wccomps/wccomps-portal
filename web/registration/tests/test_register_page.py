"""The public register page renders its fields from RegistrationForm: labels, help text and errors come from the form,
with the ids the widgets' aria-describedby names, while the edit page keeps the portal's form rows."""

import re

import pytest
from django import forms
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ..forms import RegistrationForm
from ..models import Event, Season, TeamRegistration

pytestmark = pytest.mark.django_db


@pytest.fixture
def season():
    return Season.objects.create(name="2026 Season", year=2026, is_active=True)


@pytest.fixture
def event(season):
    return Event.objects.create(
        season=season, name="Invitational 1", event_type="invitational", date="2026-01-15", registration_open=True
    )


def _page(data=None) -> str:
    url = reverse("registration_register")
    response = Client().post(url, data) if data is not None else Client().get(url)
    return response.content.decode()


def _tag(html: str, pattern: str) -> str:
    match = re.search(pattern, html)
    assert match, pattern
    return match.group(0)


def _describedby(tag: str) -> list[str]:
    return _tag(tag, r'aria-describedby="[^"]*"').split('"')[1].split()


def test_a_field_error_has_the_id_its_widget_names(event):
    html = _page({"captain_email": "not-an-email"})

    widget = _tag(html, r'<input[^>]*name="captain_email"[^>]*>')
    assert "id_captain_email_error" in _describedby(widget)
    assert _tag(html, r'<p[^>]*id="id_captain_email_error"[^>]*>[^<]*</p>').endswith(
        ">Enter a valid email address.</p>"
    )


def test_every_error_of_a_field_shows(event, monkeypatch):
    def two_errors(self):
        self.add_error("school_name", "First problem.")
        self.add_error("school_name", "Second problem.")
        return self.cleaned_data

    monkeypatch.setattr(RegistrationForm, "clean", two_errors)
    error = _tag(_page({"school_name": "Example School"}), r'<p[^>]*id="id_school_name_error"[^>]*>[^<]*</p>')

    assert "First problem." in error
    assert "Second problem." in error


def test_the_events_label_and_help_come_from_the_form(event, monkeypatch):
    monkeypatch.setattr(RegistrationForm.base_fields["events"], "label", "Example events label")
    html = _page()

    fieldset = _tag(html, r"<fieldset[^>]*>\s*<legend[^>]*>[^<]*Example events label")
    assert "id_events_helptext" not in fieldset
    assert "Select all events your team plans" not in html


def test_with_nothing_open_the_page_says_so_in_place_of_the_form(season):
    html = _page()
    message = RegistrationForm().fields["events"].help_text

    assert message == "No events are open for registration right now. Check back later."
    assert html.count(message) == 1
    assert "Registration Open" not in html
    assert 'type="submit"' not in html
    assert "<fieldset" not in html
    assert 'name="school_name"' not in html


def test_with_events_open_the_page_says_registration_is_open(event):
    html = _page()

    assert "Registration Open" in html
    assert 'type="submit"' in html
    assert "No events are open" not in html


def test_the_region_rule_shows_beside_the_events(season, event):
    qualifier = Event.objects.create(
        season=season, name="Qualifier", event_type="qualifier", date="2026-02-15", registration_open=True
    )
    html = _page({"region": "mwccdc", "events": [str(qualifier.pk)]})

    error = _tag(html, r'<p[^>]*id="id_events_error"[^>]*>[^<]*</p>')
    assert "Please remove: Qualifier" in error
    assert "id_events_error" in _describedby(_tag(html, r"<fieldset[^>]*>"))


def test_the_region_rule_shows_beside_the_events_on_the_edit_page(season, event):
    qualifier = Event.objects.create(
        season=season, name="Qualifier", event_type="qualifier", date="2026-02-15", registration_open=True
    )
    registration = TeamRegistration.objects.create(school_name="Example School", region="wrccdc")
    url = reverse("registration_edit", args=[registration.edit_token])
    html = Client().post(url, {"region": "mwccdc", "events": [str(qualifier.pk)]}).content.decode()

    error = _tag(html, r'(?s)id="id_events_error"[^>]*>.*?</')
    assert "Please remove: Qualifier" in error
    assert html.count("Please remove: Qualifier") == 1


def _summary(html: str) -> str:
    return _tag(html, r'(?s)<div[^>]*class="[^"]*error-summary[^"]*"[^>]*>.*?</div>')


def test_a_failed_submit_lists_its_errors_at_the_top_linking_to_each_field(event):
    html = _page({"captain_email": "not-an-email"})
    summary = _summary(html)

    assert 'role="alert"' in _tag(summary, r"<div[^>]*>")
    assert html.index('role="alert"') < html.index('name="school_name"')
    assert re.search(r'<a href="#id_captain_email">[^<]*Enter a valid email address.</a>', summary)
    assert 'href="#id_school_name"' in summary
    # The events are a group of checkboxes: the link goes to the first one
    assert 'href="#id_events_0"' in summary
    assert '<a href="#id_events_0">Select at least one event.</a>' in summary
    assert '<a href="#id_agree_to_rules">Agree to the competition rules to register.</a>' in summary


def test_the_summary_tells_apart_fields_that_share_a_label(event):
    summary = _summary(_page({}))

    assert "Captain email address: This field is required." in summary
    assert "Coach email address: This field is required." in summary


def test_the_summary_holds_the_forms_own_errors(event, monkeypatch):
    def form_error(self):
        raise forms.ValidationError("Example form problem.")

    monkeypatch.setattr(RegistrationForm, "clean", form_error)
    html = _page({})

    assert "Example form problem." in _summary(html)
    assert html.count("Example form problem.") == 1


def test_a_failed_submit_starts_the_title_with_error(event):
    assert _tag(_page({}), r"<title>[^<]*</title>").startswith("<title>Error: ")
    assert _tag(_page(), r"<title>[^<]*</title>").startswith("<title>Team Registration")
    assert 'role="alert"' not in _page()


def test_required_marks_are_hidden_from_screen_readers_and_explained(event):
    html = _page()

    marks = re.findall(r'<span class="required"[^>]*>\*</span>', html)
    assert marks
    assert all('aria-hidden="true"' in mark for mark in marks)
    assert re.search(r'<p class="required-note">\s*<span class="required"[^>]*>\*</span>\s*required\s*</p>', html)


def test_open_events_are_card_checkboxes_that_keep_a_tick(event):
    html = _page({"events": [str(event.pk)]})

    card = _tag(html, r"(?s)<label class=\"checkbox-item\"[^>]*>.*?</label>")
    assert re.search(rf'<input type="checkbox" name="events" value="{event.pk}"[^>]*checked', card)
    assert ">Invitational 1<" in card
    assert "January 15, 2026" in card


def test_the_edit_page_keeps_the_portal_rows(event):
    registration = TeamRegistration.objects.create(school_name="Example School", region="wrccdc")
    html = Client().get(reverse("registration_edit", args=[registration.edit_token])).content.decode()

    assert 'class="form-row"' in html
    assert "checkbox-item" not in html


def test_an_edit_form_looks_up_the_open_events_once(event):
    registration = TeamRegistration.objects.create(school_name="Example School", region="wrccdc")

    with CaptureQueriesContext(connection) as queries:
        RegistrationForm(instance=registration)

    open_filter = '"registration_event"."registration_open" AND "registration_season"."is_active"'
    assert sum(open_filter in q["sql"] for q in queries.captured_queries) == 1
