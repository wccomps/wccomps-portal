"""Template linting tests: high-value checks only.

Catches issues that are silent at runtime or hard to spot in code review.
"""

import re
from pathlib import Path

import pytest

from core.tests.guards import TEMPLATES_DIR, fail_with, template_name, tracked_templates
from core.tests.test_template_vocabulary import tags


def get_all_template_files() -> list[Path]:
    return list(tracked_templates())


def is_cotton_component(path: Path) -> bool:
    return "cotton" in path.parts


# ---------------------------------------------------------------------------
# 1. Missing {% load cotton %} → hard TemplateSyntaxError
# ---------------------------------------------------------------------------


class TestCottonImports:
    def test_cotton_components_have_load_statement(self) -> None:
        """Templates using <c-*> must have {% load cotton %}."""
        missing = []
        for path in get_all_template_files():
            if is_cotton_component(path):
                continue
            content = path.read_text()
            uses = bool(re.search(r"<c-\w+|{%\s*c-\w+", content))
            loads = bool(re.search(r"{%\s*load\s+[^%]*\bcotton\b[^%]*%}", content))
            if uses and not loads:
                missing.append(str(path.relative_to(TEMPLATES_DIR)))
        if missing:
            pytest.fail(
                "Templates using cotton components without {% load cotton %}:\n"
                + "\n".join(f"  - {t}" for t in sorted(missing))
            )


# ---------------------------------------------------------------------------
# 3. Nested <form> tags → browser validation bugs
# ---------------------------------------------------------------------------


class TestNestedForms:
    def test_no_nested_forms(self) -> None:
        """Nested <form> tags are invalid HTML and cause browser bugs."""
        issues: list[tuple[str, int]] = []
        for path in get_all_template_files():
            if is_cotton_component(path):
                continue
            content = path.read_text()
            rel = str(path.relative_to(TEMPLATES_DIR))
            depth = 0
            for match in re.finditer(r"<(/?)form[\s>]", content, re.IGNORECASE):
                if match.group(1) == "/":
                    depth = max(0, depth - 1)
                else:
                    depth += 1
                    if depth > 1:
                        line = content[: match.start()].count("\n") + 1
                        issues.append((rel, line))
        if issues:
            lines = [f"  - {p}:{ln}" for p, ln in sorted(issues)]
            pytest.fail("Nested <form> tags:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# 4. Alpine CSP compatibility → silent runtime failure
# ---------------------------------------------------------------------------


class TestAlpineCSPCompatibility:
    """Alpine CSP build only supports simple property paths and method refs.

    Expressions like !prop, a > b, fn(), a && b silently fail at runtime.
    """

    ALPINE_DIRECTIVE_RE = re.compile(
        r"(?<![:\w])"
        r"("
        r"x-(?:show|if|text|html|model|for|init|effect|bind|on)(?::[\w.\-]+)?"
        r"|@[\w.\-]+"
        r"|::[\w][\w\-]*"
        r"|:[\w][\w\-]*"
        r')\s*=\s*"([^"]*)"',
    )
    SIMPLE_EXPR_RE = re.compile(r"^[a-zA-Z_$][\w$]*(\.[a-zA-Z_$][\w$]*)*$")
    X_FOR_RE = re.compile(
        r"^\(?\s*[a-zA-Z_]\w*\s*(?:,\s*[a-zA-Z_]\w*\s*)?\)?\s+in\s+"
        r"[a-zA-Z_$][\w$]*(\.[a-zA-Z_$][\w$]*)*$"
    )

    def test_alpine_expressions_are_csp_compatible(self) -> None:
        violations: list[tuple[str, int, str, str]] = []
        for path in get_all_template_files():
            content = path.read_text()
            rel = str(path.relative_to(TEMPLATES_DIR))
            for match in self.ALPINE_DIRECTIVE_RE.finditer(content):
                directive = match.group(1)
                value = match.group(2).strip()
                if not value or "{{" in value or "{%" in value:
                    continue
                if directive == "x-for":
                    if not self.X_FOR_RE.match(value):
                        line = content[: match.start()].count("\n") + 1
                        violations.append((rel, line, directive, value))
                    continue
                if not self.SIMPLE_EXPR_RE.match(value):
                    line = content[: match.start()].count("\n") + 1
                    violations.append((rel, line, directive, value))
        if violations:
            lines = [f'  - {p}:{ln} {d}="{v}"' for p, ln, d, v in sorted(violations)]
            pytest.fail(
                "Alpine expression incompatible with CSP build "
                "(move to computed getter in Alpine.data):\n" + "\n".join(lines)
            )


# ---------------------------------------------------------------------------
# 5. No x-model → incompatible with Alpine CSP build
# ---------------------------------------------------------------------------


class TestNoXModel:
    """x-model generates assignment expressions that fail silently in CSP mode.

    Use :value/@input (or :checked/@change for checkboxes) with a setter
    method defined in Alpine.data() instead.
    """

    X_MODEL_RE = re.compile(r'x-model="([^"]*)"')

    def test_no_x_model_in_templates(self) -> None:
        """x-model must not be used — it is incompatible with Alpine CSP build."""
        violations: list[tuple[str, int, str]] = []
        for path in get_all_template_files():
            content = path.read_text()
            rel = str(path.relative_to(TEMPLATES_DIR))
            for match in self.X_MODEL_RE.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append((rel, line, match.group(1)))
        if violations:
            lines = [f'  - {p}:{ln} x-model="{v}"' for p, ln, v in sorted(violations)]
            pytest.fail(
                "x-model is incompatible with Alpine CSP build. "
                "Use :value/@input (or :checked/@change) with a setField method:\n" + "\n".join(lines)
            )


class TestMixinsComposedNotSpread:
    """Spreading a mixin ({ ...toastMixin() }) calls its getters once and copies the values, so alertClass,
    hasProgress and allSelected never changed again. compose() (static/js/utils.js) keeps them live."""

    SPREAD_RE = re.compile(r"\.\.\.\w+Mixin\(")

    def test_no_mixin_spread(self) -> None:
        violations = []
        for path in get_all_template_files():
            content = path.read_text()
            for match in self.SPREAD_RE.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append(f"  - {path.relative_to(TEMPLATES_DIR)}:{line}")
        if violations:
            pytest.fail("Combine mixins with compose(mixin(), {...}), not spread:\n" + "\n".join(violations))


class TestNoServerSideAlertVariant:
    """A cotton :prop is evaluated by Django once, at render; an Alpine variable there never takes effect.

    <c-alert :variant="messageType"> rendered every message green (the variant fell back to info), failures
    included. Messages that change in the browser use <c-toast />, whose class Alpine binds.
    """

    ALERT_VARIANT_RE = re.compile(r"<c-alert\b[^>]*\s:variant=")

    def test_no_c_alert_with_a_dynamic_variant(self) -> None:
        violations = []
        for path in get_all_template_files():
            content = path.read_text()
            for match in self.ALERT_VARIANT_RE.finditer(content):
                line = content[: match.start()].count("\n") + 1
                violations.append(f"  - {path.relative_to(TEMPLATES_DIR)}:{line}")
        if violations:
            pytest.fail(
                "Use <c-toast /> for messages Alpine sets; c-alert's :variant is fixed at render:\n"
                + "\n".join(violations)
            )


# ---------------------------------------------------------------------------
# 6. Missing {{ attrs }} → silent attribute dropping
# ---------------------------------------------------------------------------


class TestCottonAttrsPassthrough:
    """Without {{ attrs }}, Alpine directives on components are silently dropped."""

    COTTON_DIR = TEMPLATES_DIR / "cotton"
    FRAGMENT_COMPONENTS = {
        "detail_row.html",
        "messages.html",
        "pagination.html",
    }

    def test_cotton_components_include_attrs(self) -> None:
        missing = []
        for path in sorted(self.COTTON_DIR.glob("*.html")):
            if path.name in self.FRAGMENT_COMPONENTS:
                continue
            if "{{ attrs }}" not in path.read_text():
                missing.append(path.name)
        if missing:
            pytest.fail(
                "Cotton components missing {{ attrs }} (attributes silently dropped):\n"
                + "\n".join(f"  - cotton/{n}" for n in missing)
            )


# ---------------------------------------------------------------------------
# 7. An entity in a component attribute: core.cotton escapes it again
# ---------------------------------------------------------------------------

ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-f]+|[a-z][a-z0-9]*);", re.IGNORECASE)


def test_no_html_entity_in_a_component_attribute() -> None:
    """core.cotton escapes a component's attribute values, so &quot; would reach the browser as the text &quot;.
    Write the character itself, quoting the attribute with the other kind of quote if needed."""
    found = {
        f"{template_name(path)}:{tag.line}:{tag.name}:{name}": value
        for path in tracked_templates()
        for tag in tags(path)
        if tag.name.startswith("c-")
        for name, value in tag.attrs.items()
        if ENTITY_RE.search(value)
    }
    fail_with("HTML entities in component attributes", found, {})


# ---------------------------------------------------------------------------
# 8. A confirm() in an inline handler: the question is JS source, so a page value in it can end the string
# ---------------------------------------------------------------------------


def test_no_confirm_in_an_inline_handler() -> None:
    """A form asks first with data-confirm="question" (admin/base_site.html asks it, as text); an inline on*
    handler holding confirm() makes the question part of a script."""
    found = {
        f"{template_name(path)}:{tag.line}:{tag.name}:{name}": value
        for path in tracked_templates()
        for tag in tags(path)
        for name, value in tag.attrs.items()
        if name.lower().startswith("on") and "confirm(" in value
    }
    fail_with("Inline handlers that call confirm()", found, {})
