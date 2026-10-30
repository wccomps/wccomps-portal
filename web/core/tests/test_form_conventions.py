"""Static checks that keep page templates on the shared form and message conventions.

Exceptions must be visible in this file, with a reason, so they get reviewed.
"""

import ast
import re

from core.tests.guards import (
    REPO_DIR,
    component_templates,
    fail_with,
    page_templates,
    template_name,
    text,
    tracked_files,
    tracked_templates,
)

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

FORM_ERRORS_ALLOWLIST = {
    "registration/register.html": "the standalone public page, styled on its own: its summary shows the form's errors",
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


# A form's field as a bare widget, or looped over: {{ form.notes }}, {% for radio in form.source %}
BARE_FIELD = re.compile(r"{{\s*\w*form\.(\w+)\s*}}|{%\s*for\s+\w+\s+in\s+\w*form\.(\w+)\s*%}")

# "template:field" -> why the page renders that field's widget itself
BARE_FIELD_EXCEPTIONS = {
    "scoring/submit_red_finding.html:destination_ip_template": "a hidden input the page's script fills: no row to show",
}


def test_a_form_field_renders_as_its_field_group() -> None:
    """A widget rendered on its own has no label, help or errors from the form, and its aria-describedby names ids
    that aren't on the page: {{ form.x.as_field_group }} renders all of them."""
    found = {
        f"{template_name(p)}:{m.group(1) or m.group(2)}": "render it with as_field_group"
        for p in page_templates()
        for m in BARE_FIELD.finditer(text(p))
    }
    fail_with("Form fields rendered as bare widgets", found, BARE_FIELD_EXCEPTIONS)


MARKUP_CALLS = {"format_html", "format_html_join", "mark_safe", "SafeString"}


def markup_name(node: ast.AST) -> str | None:
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
    return name if name in MARKUP_CALLS else None


def test_form_markup_formats_only_literals() -> None:
    """A label or help text written as markup is shown as is: format_html escapes its values but can't stop a
    javascript: link, so a forms module calls the markup functions directly, with literals only."""
    found = {}
    for path in tracked_files():
        if not (path.startswith("web/") and path.endswith("forms.py") and "/tests/" not in path):
            continue
        tree = ast.parse((REPO_DIR / path).read_text())
        called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for node in ast.walk(tree):
            where = f"{path}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in MARKUP_CALLS and alias.asname:
                        found[where] = f"imports {alias.name} under another name"
            elif (name := markup_name(node)) and id(node) not in called:
                found[where] = f"uses {name} other than by calling it"
            elif isinstance(node, ast.Call) and (name := markup_name(node.func)):
                for value in [*node.args, *(kw.value for kw in node.keywords)]:
                    if not isinstance(value, ast.Constant):
                        found[where] = f"{name} takes {ast.unparse(value)}"
    fail_with("Form markup built from values that aren't literals", found, {})
