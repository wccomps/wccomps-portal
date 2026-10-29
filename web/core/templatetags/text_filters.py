from django import forms, template

from core.utils import shorten

register = template.Library()
register.filter("shorten", shorten)


@register.filter
def top_errors(form: forms.BaseForm) -> list[str]:
    """Non-field errors, then hidden fields' errors prefixed by label."""
    errors = [str(error) for error in form.non_field_errors()]
    return errors + [f"{field.label}: {error}" for field in form.hidden_fields() for error in field.errors]


@register.filter
def fields_with_errors(form: forms.BaseForm) -> list[tuple[str, str]]:
    """(label, id to link to) for each shown field with an error; a choice group links to its first option."""
    return [
        (str(field.label), f"{field.auto_id}_0" if field.use_fieldset else field.id_for_label)
        for field in form.visible_fields()
        if field.errors
    ]
