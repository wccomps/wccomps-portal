"""Static checks that keep page templates on the shared form and message conventions.

Exceptions must be visible in this file, with a reason, so they get reviewed.
"""

import re

from core.tests.guards import component_templates, fail_with, page_templates, template_name, text, tracked_templates

MESSAGE_LOOP = re.compile(r"{%\s*if\s+messages\s*%}|{%\s*for\s+\w+\s+in\s+messages\s*%}")

# Files allowed to loop over messages themselves, with the reason.
MESSAGE_LOOP_ALLOWLIST = {
    "admin/base_site.html": "the one place messages render",
}


def test_pages_do_not_render_messages_themselves() -> None:
    """The base template renders messages; a page loop would show every message twice."""
    found = {
        template_name(p): "loops over messages; admin/base_site.html renders them"
        for p in page_templates()
        if MESSAGE_LOOP.search(text(p))
    }
    fail_with("Pages that render messages themselves", found, MESSAGE_LOOP_ALLOWLIST)


# A form's field used on its own in a template tag: {{ form.school_name }}, {{ form.region.value }},
# {% for e in grading_form.points.errors %}
FIELD_USE = re.compile(r"{[{%][^}]*?\b\w*form\.(?!non_field_errors\b|errors\b|is_bound\b)\w+")

# Templates that render fields one by one without <c-form_errors>, with the reason.
FORM_ERRORS_ALLOWLIST = {
    "registration/register.html": "the standalone public page, styled on its own: it renders form.non_field_errors",
}


def test_forms_rendered_field_by_field_show_their_form_errors() -> None:
    """An error from a form's clean() belongs to no field: a page that renders the fields one by one and not the
    form's own errors re-renders a failed save with no reason shown."""
    found = {
        template_name(p): 'uses form fields one by one; add <c-form_errors :form="form" />'
        for p in tracked_templates()
        if p not in component_templates() and FIELD_USE.search(text(p)) and "<c-form_errors" not in text(p)
    }
    fail_with("Forms whose own errors never render", found, FORM_ERRORS_ALLOWLIST)
