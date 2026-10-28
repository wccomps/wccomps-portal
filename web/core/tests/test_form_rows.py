"""A form's field renders as a form row with its facts from the form and the ids its aria-describedby names."""

import re

from django import forms


class ExampleForm(forms.Form):
    email = forms.EmailField(label="Email Address", help_text="Where results go.")
    notes = forms.CharField(label="", required=False, help_text="Tom & Jerry's <notes>")
    phone = forms.CharField(label="Phone", required=False)
    agree = forms.BooleanField(label="I agree", help_text="Read the rules first.")
    events = forms.MultipleChoiceField(
        label="Events", choices=[("a", "A"), ("b", "B")], widget=forms.CheckboxSelectMultiple
    )


def row(form: forms.Form, name: str) -> str:
    return str(form[name].as_field_group())


def test_a_field_is_a_form_row_with_its_label_and_required_mark() -> None:
    html = row(ExampleForm(), "email")
    assert 'class="form-row"' in html
    assert re.search(r'<label for="id_email"\s+class="required">Email Address:</label>', html)
    assert 'class="required"' not in row(ExampleForm(), "phone")


def test_help_text_has_the_id_the_widget_points_at() -> None:
    html = row(ExampleForm(), "email")
    assert 'aria-describedby="id_email_helptext"' in html
    assert re.search(r'<p class="help-text" id="id_email_helptext">Where results go.</p>', html)


def test_errors_show_with_the_id_the_widget_points_at() -> None:
    html = row(ExampleForm({"email": "nope"}), "email")
    assert re.search(r'aria-describedby="id_email_helptext id_email_error"', html)
    assert re.search(r'<p class="errornote" id="id_email_error">Enter a valid email address.</p>', html)


def test_a_checkbox_comes_before_its_label() -> None:
    html = row(ExampleForm(), "agree")
    assert html.index('type="checkbox"') < html.index('<label for="id_agree"')
    assert 'id="id_agree_helptext"' in html


def test_a_choice_group_is_a_fieldset_with_a_legend() -> None:
    html = row(ExampleForm(), "events")
    assert "<fieldset" in html
    assert re.search(r"<legend[^>]*>Events:</legend>", html)
    assert '<label for="id_events"' not in html


def test_help_text_is_escaped_once() -> None:
    assert "Tom &amp; Jerry&#x27;s &lt;notes&gt;" in row(ExampleForm(), "notes")


def test_a_field_without_a_label_has_no_bare_colon() -> None:
    assert "<label" not in row(ExampleForm(), "notes")
