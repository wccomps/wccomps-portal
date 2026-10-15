"""Pages build their look from the portal's shared vocabulary, so a page can't quietly invent a style of its own.

The vocabulary is the classes the shared cotton components render or style, the utilities in app.css's marked
utilities section, and Django admin's classes. Each test lists what it finds with where it is; fix the page, or add
an exception saying why no component or utility fits. Exception keys are fnmatch patterns over "where:name", and an
exception that matches nothing fails too.

Checked: every template except the shared components themselves and standalone documents (an <html> of their own:
the 500 page, emails, the print scorecard, the public registration form), which carry their own <style> and don't
load app.css. A class counts as used where a template writes it (class="...", x-transition classes), where script
applies it (classList calls, and the getters a :class binding reads), and where Python sets it on a form widget.
"""

import fnmatch
import re
from functools import cache
from pathlib import Path

import django
import pytest

from core.tests.test_template_lint import SHARED_COMPONENTS

WEB = Path(__file__).resolve().parents[2]
TEMPLATES = WEB / "templates"
APP_CSS = WEB / "static/css/app.css"
JS_DIR = WEB / "static/js"
ADMIN_CSS = Path(django.__file__).parent / "contrib/admin/static/admin/css"
UTILITIES_RE = re.compile(r"/\* BEGIN UTILITIES \*/(.*?)/\* END UTILITIES \*/", re.DOTALL)

CLASS_NAME_RE = re.compile(r"-?[A-Za-z_][\w-]*")
COMMENT_RE = re.compile(r"{#.*?#}|{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.DOTALL)
CYCLE_RE = re.compile(r"{%\s*cycle\b(.*?)%}", re.DOTALL)
QUOTED_RE = re.compile(r"""(["'])(.*?)\1""")
CLASS_ATTR_RE = re.compile(r"""(?<![:\w-])class=(["'])(.*?)\1""", re.DOTALL)
TRANSITION_ATTR_RE = re.compile(r"""\bx-transition:(?:enter|leave)(?:-start|-end)?=(["'])(.*?)\1""")
BINDING_RE = re.compile(r"""(?:(?<![\w-])::?|x-bind:)class=(["'])([\w.$]+)\1""")
CLASSLIST_RE = re.compile(r"""classList\.(?:add|remove|toggle|replace)\(\s*(["'])([^"']+)\1""")
SELECTOR_CALL_RE = re.compile(r"""(?:querySelector(?:All)?|closest|matches)\(\s*(["'`])([^"'`]+)\1""")
WIDGET_CLASS_RE = re.compile(r"""["']class["']\s*:\s*["']([^"']+)["']""")
# A string a getter returns, or a ternary branch or property value yields
RESULT_LITERAL_RE = re.compile(r"""(?:return|\?|:|\|\|)\s*(["'])([^"']*)\1""")
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_RULE_RE = re.compile(r"([^{}]+)\{[^{}]*\}")
CSS_CLASS_RE = re.compile(r"\.(-?[A-Za-z_][\w-]*)")
RAW_TAG_RE = re.compile(r"<(button|table)\b", re.IGNORECASE)
TAG_COMPONENT = {"button": "<c-button>", "table": "<c-table>"}

NOT_SOURCE_DIRS = {"tests", "migrations", "browser_tests", "integration_tests", "staticfiles"}


# ---------------------------------------------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------------------------------------------

# "template:class" for a class no stylesheet defines and no script reads
UNDEFINED_EXCEPTIONS: dict[str, str] = {}

# "template:class" for a class app.css styles outside the shared vocabulary: page-specific styling
PAGE_STYLE_EXCEPTIONS: dict[str, str] = {
    "admin/base_site.html:*": "The portal chrome every page is framed in (nav bar, menu toggles, new-ticket toast)",
    "admin/broadcast.html:team-list": "The recipient list flows into 2 to 4 columns by width; no utility sets columns",
    "admin/category_form.html:checkbox-group": "Stacks the field checkboxes 8px apart; no utility stacks with that gap",
    "*:comment-*": "Ticket comments (ticket_detail and the ops partial it loads): a thread of bordered notes",
    "*:history-*": "Ticket history: a table on wide screens, cards on phones; only the ticket page has history",
    "ticket_detail.html:attachment-*": "Attachment thumbnails; c-image_grid is a fixed-column grid of bare images",
    "ticket_detail.html:pdf-icon": "Ticket attachment preview for a PDF",
    "ticket_detail.html:file-icon": "Ticket attachment preview for another file type",
    "ticket_detail.html:playbook-steps": "The category playbook's numbered steps on the ticket page",
    "scoring/_scorecard_body.html:*": "The team scorecard: its neighbours column, insights and total row",
    "scoring/scorecard.html:scorecard": "The scorecard's print layout (font size, no page breaks inside tables)",
    "*:row-below-avg": "A red-tinted row (a scorecard category below average, a team with no email address)",
    "scoring/leaderboard.html:show-schools": "Switches the leaderboard between team and school names",
    "scoring/match_incident.html:module-header*": (
        "The match page's two panels carry a full-width title bar, green for the matching side; c-module's heading "
        "is Django's compact bar"
    ),
    "scoring/partials/inject_feedback_table.html:click-to-edit": "Feedback edited in place; no other table does",
    "scoring/scoring_config.html:max-points-grid": (
        "The weight and maximum fields side by side, wrapping below 200px each; grid-2 and grid-4 have fixed columns"
    ),
    "scoring/submit_*.html:screenshot-preview-*": "Previews of the screenshots chosen for upload on the report forms",
    "scoring/submit_red_finding.html:modal*": "The Create IP Pool dialog, the portal's only modal",
    "scoring/submit_red_finding.html:team-btn": "The team picker's toggle buttons: outlined, red when picked",
}

# "template:button" or "template:table" for a raw tag outside cotton/
RAW_TAG_EXCEPTIONS: dict[str, str] = {
    "admin/base_site.html:button": (
        "The chrome's menu, dark mode and sound toggles; c-button would put Django's filled .button on the nav bar"
    ),
    "scoring/submit_red_finding.html:button": (
        "The team picker and the dialog's close button have their own look; c-button would add Django's filled .button"
    ),
}

# "app.css:class" for a class app.css defines that no template, script or Python source uses
UNUSED_CSS_EXCEPTIONS: dict[str, str] = {
    "app.css:htmx-request": "htmx adds it to an element while its request runs",
}


# ---------------------------------------------------------------------------------------------------------------
# Reading templates, scripts and stylesheets
# ---------------------------------------------------------------------------------------------------------------


def rel(path: Path) -> str:
    return (
        path.relative_to(TEMPLATES).as_posix() if path.is_relative_to(TEMPLATES) else path.relative_to(WEB).as_posix()
    )


@cache
def text(path: Path) -> str:
    """The file without template comments, keeping their line breaks so line numbers still match."""
    return COMMENT_RE.sub(lambda m: "\n" * m.group().count("\n"), path.read_text(encoding="utf-8"))


@cache
def all_templates() -> tuple[Path, ...]:
    return tuple(sorted(TEMPLATES.rglob("*.html")))


@cache
def checked_templates() -> tuple[Path, ...]:
    """Pages and page partials: not the shared components, not standalone documents with a stylesheet of their own."""
    return tuple(
        p
        for p in all_templates()
        if not (p.parent.name == "cotton" and p.name in SHARED_COMPONENTS) and not re.search(r"<html\b", text(p), re.I)
    )


@cache
def shared_components() -> tuple[Path, ...]:
    return tuple(TEMPLATES / "cotton" / name for name in sorted(SHARED_COMPONENTS))


@cache
def scripts() -> tuple[Path, ...]:
    """Every file whose script can apply or read classes: templates (inline <script>) and static/js."""
    return all_templates() + tuple(sorted(JS_DIR.glob("*.js")))


@cache
def python_sources() -> tuple[Path, ...]:
    return tuple(p for p in sorted(WEB.rglob("*.py")) if not NOT_SOURCE_DIRS & set(p.relative_to(WEB).parts))


def attribute_classes(value: str) -> set[str]:
    """The class names in a class attribute: {% %} tags separate names, a {{ }} makes the name around it partial.

    A {% cycle %} writes its quoted arguments.
    """
    cycled = {
        name for tag in CYCLE_RE.findall(value) for _, arg in QUOTED_RE.findall(tag) for name in literal_classes(arg)
    }
    value = re.sub(r"{%.*?%}", " ", value, flags=re.DOTALL)
    value = re.sub(r"{{.*?}}", "\0", value, flags=re.DOTALL)
    return cycled | {token for token in value.split() if "\0" not in token and CLASS_NAME_RE.fullmatch(token)}


def written_classes(path: Path) -> set[str]:
    """Classes a template writes in its markup: class attributes (on elements and components) and x-transition."""
    content = text(path)
    names = set()
    for _, value in CLASS_ATTR_RE.findall(content):
        names |= attribute_classes(value)
    for _, value in TRANSITION_ATTR_RE.findall(content):
        names |= attribute_classes(value)
    return names


@cache
def bound_names() -> frozenset[str]:
    """The properties :class bindings read, across every template (r.rowClass reads rowClass)."""
    return frozenset(m.group(2).split(".")[-1] for p in all_templates() for m in BINDING_RE.finditer(text(p)))


def literal_classes(literal: str) -> set[str]:
    return {token for token in literal.split() if CLASS_NAME_RE.fullmatch(token)}


def applied_classes(path: Path, names: frozenset[str] | None = None) -> set[str]:
    """Classes a file's script applies: classList calls, and what the getters and properties :class reads yield."""
    content = text(path)
    found = set()
    for m in CLASSLIST_RE.finditer(content):
        found |= literal_classes(m.group(2))
    for name in sorted(bound_names() if names is None else names):
        for body in re.findall(rf"\bget\s+{name}\s*\(\)\s*\{{(.*?)\}}", content, re.DOTALL):
            found |= {c for m in RESULT_LITERAL_RE.finditer(body) for c in literal_classes(m.group(2))}
        for value in re.findall(rf"\b{name}\s*:\s*([^,\n]+)", content):
            found |= {c for m in RESULT_LITERAL_RE.finditer(":" + value) for c in literal_classes(m.group(2))}
    return found


@cache
def uses() -> dict[str, set[str]]:
    """Class -> the files (templates, scripts, Python) that put it on a checked page."""
    found: dict[str, set[str]] = {}
    checked = set(checked_templates())
    for path in scripts():
        names = (written_classes(path) if path in checked else set()) | applied_classes(path)
        for name in names:
            found.setdefault(name, set()).add(rel(path))
    for path in python_sources():
        for value in WIDGET_CLASS_RE.findall(path.read_text(encoding="utf-8")):
            for name in literal_classes(value):
                found.setdefault(name, set()).add(rel(path))
    return found


@cache
def hooks() -> frozenset[str]:
    """Classes script reads to find elements (querySelector('.box-toggle')): hooks with no style of their own."""
    return frozenset(
        name
        for path in scripts()
        for m in SELECTOR_CALL_RE.finditer(text(path))
        for name in CSS_CLASS_RE.findall(m.group(2))
    )


def css_rules(css: str) -> list[list[str]]:
    """Each selector's class names, in order, one list per comma-separated selector."""
    css = CSS_COMMENT_RE.sub(" ", css)
    return [CSS_CLASS_RE.findall(selector) for m in CSS_RULE_RE.finditer(css) for selector in m.group(1).split(",")]


@cache
def app_css_classes() -> frozenset[str]:
    return frozenset(name for selector in css_rules(APP_CSS.read_text()) for name in selector)


@cache
def admin_css_classes() -> frozenset[str]:
    return frozenset(
        name for path in ADMIN_CSS.rglob("*.css") for selector in css_rules(path.read_text()) for name in selector
    )


@cache
def utility_classes() -> frozenset[str]:
    sections = UTILITIES_RE.findall(APP_CSS.read_text())
    return frozenset(name for section in sections for selector in css_rules(section) for name in selector)


@cache
def component_classes() -> frozenset[str]:
    """Classes the shared components render, plus the ones app.css styles inside them (.action-box .form-group)."""
    rendered: set[str] = set()
    for path in shared_components():
        rendered |= written_classes(path)
        names = frozenset(m.group(2).split(".")[-1] for m in BINDING_RE.finditer(text(path)))
        for script in scripts():
            rendered |= applied_classes(script, names)
    roots = rendered - utility_classes()
    styled = {
        name for selector in css_rules(APP_CSS.read_text()) if selector and selector[0] in roots for name in selector
    }
    return frozenset(rendered | styled)


# ---------------------------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------------------------


def fail_with(title: str, found: dict[str, str], exceptions: dict[str, str]) -> None:
    """Fail listing each found key (with its detail) no exception covers, and each exception that covers nothing."""
    matched = {pattern for key in found for pattern in exceptions if fnmatch.fnmatchcase(key, pattern)}
    problems = sorted(
        f"{key}{f' ({detail})' if detail else ''}"
        for key, detail in found.items()
        if not any(fnmatch.fnmatchcase(key, pattern) for pattern in exceptions)
    )
    problems += [
        f"{pattern}: in the exceptions but matches nothing; drop it there"
        for pattern in exceptions
        if pattern not in matched
    ]
    if problems:
        pytest.fail(f"{title} ({len(problems)}):\n" + "\n".join(f"  {p}" for p in problems), pytrace=False)


def by_use(names: set[str]) -> dict[str, str]:
    return {f"{where}:{name}": "" for name in names for where in uses()[name]}


# ---------------------------------------------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------------------------------------------


def test_utilities_section_is_marked() -> None:
    assert len(UTILITIES_RE.findall(APP_CSS.read_text())) == 1, (
        "app.css needs one BEGIN UTILITIES/END UTILITIES section"
    )


def test_every_class_is_defined() -> None:
    """A class no stylesheet defines does nothing (no Bootstrap is loaded), unless script reads it as a hook."""
    defined = app_css_classes() | admin_css_classes() | hooks()
    fail_with(
        "Classes no stylesheet defines: define it in app.css, use a component or utility, or drop it",
        by_use(set(uses()) - defined),
        UNDEFINED_EXCEPTIONS,
    )


def test_classes_come_from_the_shared_vocabulary() -> None:
    """app.css classes a page uses come from a shared component or the utilities, not a style the page invented."""
    vocabulary = component_classes() | utility_classes() | admin_css_classes()
    fail_with(
        "Page-specific styling: use a cotton component or an app.css utility, or add an exception saying why none fits",
        by_use((set(uses()) & app_css_classes()) - vocabulary),
        PAGE_STYLE_EXCEPTIONS,
    )


def test_no_raw_buttons_or_tables() -> None:
    lines: dict[str, list[str]] = {}
    for path in checked_templates():
        content = text(path)
        for m in RAW_TAG_RE.finditer(content):
            line = content[: m.start()].count("\n") + 1
            lines.setdefault(f"{rel(path)}:{m.group(1).lower()}", []).append(str(line))
    found = {key: f"use {TAG_COMPONENT[key.rsplit(':', 1)[1]]}; line {', '.join(ln)}" for key, ln in lines.items()}
    fail_with("Raw <button>/<table> outside cotton/", found, RAW_TAG_EXCEPTIONS)


def test_every_app_css_class_is_used() -> None:
    """A rule nothing uses is dead weight that misleads the next reader of app.css."""
    words: set[str] = set()
    for path in (*all_templates(), *sorted(JS_DIR.glob("*.js")), *python_sources()):
        words |= set(re.findall(r"[\w-]+", path.read_text(encoding="utf-8")))
    fail_with(
        "app.css classes nothing uses: remove the rule",
        {f"app.css:{name}": "" for name in app_css_classes() - words},
        UNUSED_CSS_EXCEPTIONS,
    )
