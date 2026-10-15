"""Template linting tests — high-value checks only.

Catches issues that are silent at runtime or hard to spot in code review.
"""

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"


# The shared components: the portal's building blocks, whose classes make up the vocabulary pages style with
# (test_template_vocabulary). Every .html in cotton/ is in this set or PAGE_PARTIALS.
SHARED_COMPONENTS = {
    # Enforced: have a RAW_HTML_PATTERNS entry
    "action_box.html",
    "alert.html",
    "badge.html",
    "button_row.html",
    "detail_grid.html",
    "empty_state.html",
    "filter_field.html",
    "form_field.html",
    "info_box.html",
    "module.html",
    "progress_bar.html",
    # Exempt: no unique detectable class
    "button.html",
    "detail_row.html",
    "fieldset.html",
    "filter_toolbar.html",
    "form.html",
    "image_grid.html",
    "link.html",
    "messages.html",
    "nav.html",
    "nav_item.html",
    "page_header.html",
    "pagination.html",
    "score_value.html",
    "table.html",
    "table_header.html",
    "toast.html",
}
# One page's markup kept in cotton/, not shared: test_template_vocabulary checks these like pages
PAGE_PARTIALS = {
    "inject_grades_table.html",
    "inject_grading_content.html",
    "red_findings_table.html",
    "registration_review_table.html",
    "review_incidents_table.html",
    "review_orange_table.html",
    "review_tickets_table.html",
    "ticket_list_table.html",
}


def get_all_template_files() -> list[Path]:
    return list(TEMPLATES_DIR.rglob("*.html"))


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
# 2. Raw HTML where cotton components exist → inconsistency
# ---------------------------------------------------------------------------


class TestCottonComponentUsage:
    """Detect raw HTML that should use a cotton component.

    test_all_components_registered ensures this stays exhaustive:
    adding a new .html to cotton/ without registering it fails the build.
    """

    COTTON_DIR = TEMPLATES_DIR / "cotton"

    # Every .html in cotton/ must be in one of these sets.
    REGISTERED_COMPONENTS = SHARED_COMPONENTS | PAGE_PARTIALS

    RAW_HTML_PATTERNS = [
        (r'<span[^>]*class="[^"]*\bbadge\b[^"]*"[^>]*>', "<c-badge>"),
        (r'<div[^>]*class="[^"]*\bempty-state\b[^"]*"[^>]*>', "<c-empty_state>"),
        (r'<div[^>]*class="[^"]*\bmodule(?![\w-])[^"]*"[^>]*>', "<c-module>"),
        (r'<div[^>]*class="[^"]*\bsubmit-row\b[^"]*"[^>]*>', "<c-button_row>"),
        (r'<div[^>]*class="[^"]*\baction-box\b[^"]*"[^>]*>', "<c-action_box>"),
        (r'<div[^>]*class="[^"]*\binfo-box\b[^"]*"[^>]*>', "<c-info_box>"),
        (r'<div[^>]*class="[^"]*\balert\b[^"]*"[^>]*>', "<c-alert>"),
        (r'<div[^>]*class="[^"]*\bprogress-bar\b[^"]*"[^>]*>', "<c-progress_bar>"),
        (r'<div[^>]*class="[^"]*\bform-row\b[^"]*"[^>]*>', "<c-form_field>"),
        (r'<div[^>]*class="[^"]*\bfilter-field\b[^"]*"[^>]*>', "<c-filter_field>"),
        (r'<dl[^>]*class="[^"]*\bdetail-list\b[^"]*"[^>]*>', "<c-detail_grid>"),
    ]

    # (relative_path, component) pairs to skip — Alpine dynamic binding, etc.
    KNOWN_VIOLATIONS = {
        ("scoring/email_scorecards_confirm.html", "<c-alert>"),
        ("admin/broadcast.html", "<c-form_field>"),
        ("orange_team/check_detail.html", "<c-form_field>"),
        ("orange_team/check_form.html", "<c-form_field>"),
    }

    def _should_skip(self, path: Path) -> bool:
        if is_cotton_component(path):
            return True
        if "emails" in path.parts or "registration" in path.parts:
            return True
        # Admin templates that don't use cotton are Django admin overrides
        if "admin" in path.parts:
            content = path.read_text()
            if not re.search(r"{%\s*load\s+[^%]*\bcotton\b[^%]*%}", content):
                return True
        return "pdf" in path.name.lower() or "scorecard" in path.name.lower()

    def test_no_raw_html_patterns(self) -> None:
        """Templates should use cotton components instead of raw HTML."""
        issues: list[tuple[str, int, str]] = []
        for path in get_all_template_files():
            if self._should_skip(path):
                continue
            content = path.read_text()
            rel = str(path.relative_to(TEMPLATES_DIR))
            for pattern, component in self.RAW_HTML_PATTERNS:
                if (rel, component) in self.KNOWN_VIOLATIONS:
                    continue
                for match in re.finditer(pattern, content, re.IGNORECASE | re.DOTALL):
                    line = content[: match.start()].count("\n") + 1
                    issues.append((rel, line, component))
        if issues:
            lines = [f"  - {p}:{ln} -> use {c}" for p, ln, c in sorted(issues)]
            pytest.fail("Raw HTML — use cotton component:\n" + "\n".join(lines))

    def test_all_components_registered(self) -> None:
        """New cotton components must be registered."""
        actual = {f.name for f in self.COTTON_DIR.glob("*.html")}
        unknown = actual - self.REGISTERED_COMPONENTS
        deleted = self.REGISTERED_COMPONENTS - actual
        if unknown:
            pytest.fail(
                "Unregistered cotton component(s) — add to REGISTERED_COMPONENTS "
                "(and RAW_HTML_PATTERNS if it has a unique CSS class):\n"
                + "\n".join(f"  - cotton/{n}" for n in sorted(unknown))
            )
        if deleted:
            pytest.fail(
                "Stale REGISTERED_COMPONENTS entry (file deleted):\n"
                + "\n".join(f"  - cotton/{n}" for n in sorted(deleted))
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
