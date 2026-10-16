"""Template linting tests: high-value checks only.

Catches issues that are silent at runtime or hard to spot in code review.
"""

import re

from core.tests.guards import (
    Tag,
    component_templates,
    fail_with,
    page_templates,
    tags,
    template_name,
    text,
    tracked_templates,
    with_open_elements,
)

# ---------------------------------------------------------------------------
# 1. Missing {% load cotton %}: a hard TemplateSyntaxError
# ---------------------------------------------------------------------------


def test_cotton_components_have_load_statement() -> None:
    """Templates using <c-*> must have {% load cotton %}."""
    found = {}
    for path in page_templates():
        content = text(path)
        uses = bool(re.search(r"<c-\w+|{%\s*c-\w+", content))
        if uses and not re.search(r"{%\s*load\s+[^%]*\bcotton\b[^%]*%}", content):
            found[template_name(path)] = "uses cotton components without {% load cotton %}"
    fail_with("Missing {% load cotton %}", found, {})


# ---------------------------------------------------------------------------
# 3. Nested <form> tags: browser validation bugs
# ---------------------------------------------------------------------------


def test_no_nested_forms() -> None:
    """Nested <form> tags are invalid HTML: the browser drops the inner form, so its button submits the outer one."""
    found = {
        f"{template_name(path)}:{tag.line}": "a form inside a form"
        for path in page_templates()
        for tag, open_tags in with_open_elements(path)
        if tag.name in ("form", "c-form") and any(t.name in ("form", "c-form") for t in open_tags)
    }
    fail_with("Nested forms", found, {})


# ---------------------------------------------------------------------------
# 4. Alpine CSP compatibility: silent runtime failure
# ---------------------------------------------------------------------------


class TestAlpineCSPCompatibility:
    """Alpine CSP build only supports simple property paths and method refs.

    Expressions like !prop, a > b, fn(), a && b silently fail at runtime.
    """

    ALPINE_DIRECTIVE_RE = re.compile(
        r"x-(?:show|if|text|html|model|for|init|effect|bind|on)(?::[\w.\-]+)?|@[\w.\-]+|::?[\w][\w\-]*"
    )
    SIMPLE_EXPR_RE = re.compile(r"^[a-zA-Z_$][\w$]*(\.[a-zA-Z_$][\w$]*)*$")
    X_FOR_RE = re.compile(
        r"^\(?\s*[a-zA-Z_]\w*\s*(?:,\s*[a-zA-Z_]\w*\s*)?\)?\s+in\s+"
        r"[a-zA-Z_$][\w$]*(\.[a-zA-Z_$][\w$]*)*$"
    )

    def test_alpine_expressions_are_csp_compatible(self) -> None:
        found = {}
        for path in tracked_templates():
            for tag in tags(path):
                for directive, raw in tag.attrs.items():
                    value = raw.strip()
                    if not self.ALPINE_DIRECTIVE_RE.fullmatch(directive) or not value or "{{" in value or "{%" in value:
                        continue
                    pattern = self.X_FOR_RE if directive == "x-for" else self.SIMPLE_EXPR_RE
                    if not pattern.match(value):
                        found[f'{template_name(path)}:{tag.line} {directive}="{value}"'] = ""
        fail_with("Alpine expressions the CSP build can't run (move them to a getter in Alpine.data)", found, {})


# ---------------------------------------------------------------------------
# 5. No x-model: incompatible with the Alpine CSP build
# ---------------------------------------------------------------------------


class TestNoXModel:
    """x-model generates assignment expressions that fail silently in CSP mode.

    Use :value/@input (or :checked/@change for checkboxes) with a setter
    method defined in Alpine.data() instead.
    """

    def test_no_x_model_in_templates(self) -> None:
        """x-model must not be used: it is incompatible with the Alpine CSP build."""
        found = {
            f'{template_name(path)}:{tag.line} x-model="{value}"': ""
            for path in tracked_templates()
            for tag in tags(path)
            for name, value in tag.attrs.items()
            if name.split(".")[0] == "x-model"
        }
        fail_with("x-model (use :value/@input or :checked/@change with a setter method)", found, {})


class TestMixinsComposedNotSpread:
    """Spreading a mixin ({ ...toastMixin() }) calls its getters once and copies the values, so alertClass,
    hasProgress and allSelected never changed again. compose() (static/js/utils.js) keeps them live."""

    SPREAD_RE = re.compile(r"\.\.\.\w+Mixin\(")

    def test_no_mixin_spread(self) -> None:
        found = {}
        for path in tracked_templates():
            content = text(path)
            for match in self.SPREAD_RE.finditer(content):
                found[f"{template_name(path)}:{content.count(chr(10), 0, match.start()) + 1}"] = ""
        fail_with("Mixins spread rather than combined with compose(mixin(), {...})", found, {})


class TestNoServerSideAlertVariant:
    """A cotton :prop is evaluated by Django once, at render; an Alpine variable there never takes effect.

    <c-alert :variant="messageType"> rendered every message green (the variant fell back to info), failures
    included. Messages that change in the browser use <c-toast />, whose class Alpine binds.
    """

    def test_no_c_alert_with_a_dynamic_variant(self) -> None:
        found = {
            f"{template_name(path)}:{tag.line}": ""
            for path in tracked_templates()
            for tag in tags(path)
            if tag.name == "c-alert" and ":variant" in tag.attrs
        }
        fail_with("c-alert with :variant (fixed at render; use <c-toast /> for messages Alpine sets)", found, {})


# ---------------------------------------------------------------------------
# 6. Missing {{ attrs }}: attributes silently dropped
# ---------------------------------------------------------------------------


# Components that don't write {{ attrs }}, and why nothing needs to pass them attributes
FRAGMENT_COMPONENTS = {
    "cotton/detail_row.html": "a row of c-detail_grid, which carries the grid's attributes",
    "cotton/messages.html": "the chrome's message list, rendered once by admin/base_site.html",
    "cotton/pagination.html": "a paragraph of page links built from page_obj alone",
}


def test_cotton_components_include_attrs() -> None:
    """Without {{ attrs }}, Alpine directives on components are silently dropped."""
    found = {
        template_name(path): "writes no {{ attrs }}"
        for path in component_templates()
        if path.suffix == ".html" and "{{ attrs }}" not in text(path)
    }
    fail_with("Components that drop the attributes passed to them", found, FRAGMENT_COMPONENTS)


# ---------------------------------------------------------------------------
# 7. An entity in a component attribute: core.cotton escapes it again
# ---------------------------------------------------------------------------

ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-f]+|[a-z][a-z0-9]*);", re.IGNORECASE)


def test_no_html_entity_in_a_component_attribute() -> None:
    """core.cotton escapes a plain literal component attribute, so &quot; in one reaches the browser as the text
    &quot;: write the character. A value with template tags is rendered as HTML, so its entities stand."""
    found = {
        f"{template_name(path)}:{tag.line}:{tag.name}:{name}": value
        for path in tracked_templates()
        for tag in tags(path)
        if tag.name.startswith("c-")
        for name, value in tag.attrs.items()
        if ENTITY_RE.search(value) and "{{" not in value and "{%" not in value
    }
    fail_with("HTML entities in component attributes", found, {})


# ---------------------------------------------------------------------------
# 8. A confirm() in an inline handler: the question is JS source, so a page value in it can end the string
# ---------------------------------------------------------------------------


def test_no_confirm_in_an_inline_handler() -> None:
    """A form asks first with data-confirm="question" (static/js/utils.js asks it, as text); an inline on*
    handler holding confirm() makes the question part of a script."""
    found = {
        f"{template_name(path)}:{tag.line}:{tag.name}:{name}": value
        for path in tracked_templates()
        for tag in tags(path)
        for name, value in tag.attrs.items()
        if name.lower().startswith("on") and "confirm(" in value
    }
    fail_with("Inline handlers that call confirm()", found, {})


PAGE_VALUE_RE = re.compile(r"[A-Za-z_][\w.]*")


def page_value_in_handler(tag: Tag, name: str, value: str) -> bool:
    """An on* attribute that carries a page value: written {{ }} or {% %}, bound with :on*, or, on a component, a bare
    name (no handler is one; cotton reads an unquoted one from the context, and the tag reader can't see quotes)."""
    if not name.lstrip(":").lower().startswith("on"):
        return False
    if name.startswith(":") or "{{" in value or "{%" in value:
        return True
    return tag.name.startswith("c-") and bool(PAGE_VALUE_RE.fullmatch(value))


def test_no_page_value_in_an_inline_handler() -> None:
    """An on* attribute is script, which HTML escaping doesn't make safe: a page value goes in a data-* attribute
    that an Alpine.data component reads as text."""
    found = {
        f"{template_name(path)}:{tag.line}:{tag.name}:{name}": value
        for path in tracked_templates()
        for tag in tags(path)
        for name, value in tag.attrs.items()
        if page_value_in_handler(tag, name, value)
    }
    fail_with("Page values inside inline handlers", found, {})


def test_a_question_is_data_confirm_on_a_form() -> None:
    """static/js/utils.js asks a form's data-confirm before it submits; on any other element it asks nothing, and
    hx-confirm asks only before an htmx request, which no form makes."""
    found = {}
    for path in tracked_templates():
        for tag in tags(path):
            where = f"{template_name(path)}:{tag.line}:{tag.name}"
            if "hx-confirm" in tag.attrs:
                found[where] = "hx-confirm (put the question in its form's data-confirm)"
            confirm = [name for name in tag.attrs if name.lstrip(":") == "data-confirm"]
            if confirm and tag.name not in ("form", "c-form"):
                found[where] = "data-confirm on something other than a form"
    fail_with("Questions asked somewhere they aren't", found, {})
