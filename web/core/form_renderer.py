from django import forms
from django.forms.renderers import TemplatesSetting
from django.forms.utils import pretty_name
from django.utils.html import strip_tags


class PortalFormRenderer(TemplatesSetting):
    """Renders through the project's template engine, so a form row can use the portal's templates."""

    field_template_name = "forms/field.html"


def top_errors(form: forms.BaseForm) -> list[str]:
    """The form's own errors, and its hidden fields' (which have no row to show them beside) named by label."""
    errors = [str(error) for error in form.non_field_errors()]
    return errors + [f"{field.label}: {error}" for field in form.hidden_fields() for error in field.errors]


def fields_with_errors(form: forms.BaseForm) -> list[tuple[forms.BoundField, str, str]]:
    """Each shown field with an error, its label and link target; a group with no label target links to option 0."""
    labels = [strip_tags(str(field.label)) for field in form.visible_fields()]
    found = []
    for field in form.visible_fields():
        if field.errors:
            label = strip_tags(str(field.label))
            if labels.count(label) > 1:
                label = pretty_name(field.name)
            found.append((field, label, field.id_for_label or f"{field.auto_id}_0"))
    return found


def ask_for_the_file_again(form: forms.BaseForm, field: str) -> None:
    """A browser can't put a file back in its input, so a form shown again over an accepted file says to pick it."""
    if field in form.cleaned_data:
        form.add_error(field, "Pick the file again: a page shown again with errors can't keep it.")
