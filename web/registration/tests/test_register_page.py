"""The public register page renders its fields from RegistrationForm: labels, help text and errors come from the form,
with the ids the widgets' aria-describedby names, while the edit page keeps the portal's form rows."""

import re

import pytest
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


def test_the_empty_events_message_is_the_fields_help_text(season):
    html = _page()
    message = RegistrationForm().fields["events"].help_text

    assert message
    assert "id_events_helptext" in _describedby(_tag(html, r"<fieldset[^>]*>"))
    assert _tag(html, r'<p[^>]*id="id_events_helptext"[^>]*>[^<]*</p>').endswith(f">{message}</p>")
    assert html.count(message) == 1
    assert 'class="checkbox-item' not in html


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
