from django import forms, template

from core.utils import shorten

register = template.Library()
register.filter("shorten", shorten)


@register.filter
def top_errors(form: forms.BaseForm) -> list[str]:
    """The form's own errors, and its hidden fields' (which have no row to show them beside) named by label."""
    errors = [str(error) for error in form.non_field_errors()]
    return errors + [f"{field.label}: {error}" for field in form.hidden_fields() for error in field.errors]


@register.filter
def fields_with_errors(form: forms.BaseForm) -> list[tuple[str, str]]:
    """(label, id to link to) for each shown field with an error."""
    from core import forms as portal_forms

    return [(label, anchor) for _, label, anchor in portal_forms.fields_with_errors(form)]
