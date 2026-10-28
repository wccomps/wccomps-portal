"""c-form_field given a bound field takes its label, label target, required mark, help text and errors from the form;
a declared prop the tag doesn't pass stays empty."""

from django import forms
from django.template import Context, Template
from django_cotton.compiler_regex import CottonCompiler


class ExampleForm(forms.Form):
    email = forms.EmailField(label="Email Address", help_text="Where results go.")
    phone = forms.CharField(label="Phone", required=False)


def render(source: str, form: forms.Form) -> str:
    return Template(CottonCompiler().process("{% load cotton %}" + source)).render(Context({"form": form}))


def test_a_bound_field_supplies_label_target_required_help_and_errors() -> None:
    html = render('<c-form_field :field="form.email">{{ form.email }}</c-form_field>', ExampleForm({"email": "nope"}))
    assert '<label for="id_email"' in html
    assert 'class="required"' in html
    assert "Email Address:" in html
    assert "Where results go." in html
    assert '<p class="errornote">Enter a valid email address.</p>' in html


def test_an_optional_field_is_not_marked_required() -> None:
    html = render('<c-form_field :field="form.phone">{{ form.phone }}</c-form_field>', ExampleForm())
    assert 'class="required"' not in html
    assert "errornote" not in html


def test_a_label_or_help_text_overrides_the_field_s() -> None:
    html = render(
        '<c-form_field :field="form.email" label="Contact" help_text="Shown instead">{{ form.email }}</c-form_field>',
        ExampleForm(),
    )
    assert "Contact:" in html
    assert "Shown instead" in html
    assert "Where results go." not in html


def test_an_unpassed_prop_ignores_a_page_variable_of_its_name() -> None:
    html = Template(CottonCompiler().process("{% load cotton %}<c-module>x</c-module>")).render(
        Context({"title": "From the view"})
    )
    assert "From the view" not in html
