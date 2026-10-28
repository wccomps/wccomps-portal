from django import forms, template

from core.utils import shorten

register = template.Library()
register.filter("shorten", shorten)


@register.filter
def top_errors(form: object) -> list[str]:
    """The form's own errors and its hidden fields', which have no row to show them beside."""
    if not isinstance(form, forms.BaseForm):
        return []
    errors = [str(error) for error in form.non_field_errors()]
    return errors + [str(error) for field in form.hidden_fields() for error in field.errors]
