"""The form renderer: field.as_field_group renders a field as a portal form row (templates/forms/field.html)."""

from django.forms.renderers import TemplatesSetting


class PortalFormRenderer(TemplatesSetting):
    """Renders through the project's template engine, so a form row can use the portal's templates."""

    field_template_name = "forms/field.html"
