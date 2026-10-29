"""A form's field renders as a form row with its facts from the form and the ids its aria-describedby names."""

import re

from django import forms
from django.utils.html import format_html

from core.widgets import ToggleButtonSelectMultiple


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


class HiddenFieldForm(forms.Form):
    season = forms.IntegerField(widget=forms.HiddenInput)


def test_a_hidden_field_s_error_shows_with_the_form_s_own_named_by_label() -> None:
    from django.template import Context, Template
    from django_cotton.compiler_regex import CottonCompiler

    template = Template(CottonCompiler().process('{% load cotton %}<c-form_errors :form="form" />'))
    html = template.render(Context({"form": HiddenFieldForm({"season": "x"})}))
    assert "Season: Enter a whole number." in html


def test_the_form_errors_box_links_each_field_with_an_error() -> None:
    from django.template import Context, Template
    from django_cotton.compiler_regex import CottonCompiler

    template = Template(CottonCompiler().process('{% load cotton %}<c-form_errors :form="form" />'))
    html = template.render(Context({"form": ExampleForm({"email": "nope"})}))
    assert '<a href="#id_email">Email Address</a>' in html
    assert '<a href="#id_agree">I agree</a>' in html
    assert '<a href="#id_events_0">Events</a>' in html
    assert "#id_phone" not in html


class TogglePickerForm(forms.Form):
    teams = forms.MultipleChoiceField(
        label="Teams", choices=[("1", "Team 1"), ("2", "Team 2")], widget=ToggleButtonSelectMultiple()
    )
    rules = forms.BooleanField(label=format_html('I agree to the <a href="{}">rules</a>', "https://example.com/r"))
    captain_email = forms.EmailField(label="Email")
    coach_email = forms.EmailField(label="Email")


def test_every_error_link_reaches_an_element_that_takes_focus() -> None:
    from django.template import Context, Template
    from django_cotton.compiler_regex import CottonCompiler

    form = TogglePickerForm({})
    errors = Template(CottonCompiler().process('{% load cotton %}<c-form_errors :form="form" />')).render(
        Context({"form": form})
    )
    page = errors + "".join(str(form[name].as_field_group()) for name in form.fields)
    for anchor in re.findall(r'href="#([^"]+)"', errors):
        assert re.search(
            rf'id="{anchor}"[^>]*tabindex|tabindex[^>]*id="{anchor}"|<(input|select|textarea)[^>]*id="{anchor}"', page
        ), anchor


def test_error_links_hold_no_link_and_tell_repeated_labels_apart() -> None:
    from django.template import Context, Template
    from django_cotton.compiler_regex import CottonCompiler

    html = Template(CottonCompiler().process('{% load cotton %}<c-form_errors :form="form" />')).render(
        Context({"form": TogglePickerForm({})})
    )
    assert "example.com/r" not in html
    assert ">I agree to the rules<" in html
    assert ">Captain email<" in html and ">Coach email<" in html
