from django import forms, template

from core.utils import shorten

register = template.Library()
register.filter("shorten", shorten)


@register.filter
def top_errors(form: forms.BaseForm) -> list[str]:
    """The form's own errors, and its hidden fields' (which have no row to show them beside) named by label."""
    errors = [str(error) for error in form.non_field_errors()]
    return errors + [f"{field.label}: {error}" for field in form.hidden_fields() for error in field.errors]
