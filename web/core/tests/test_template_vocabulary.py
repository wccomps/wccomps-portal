"""Pages build their look from the portal's shared vocabulary, so a page can't quietly invent a style of its own.

The vocabulary is the classes the shared cotton components render (with their app.css modifiers, X--* and
X__*), the utilities in app.css's marked section, and Django admin's classes. A class a page's own script applies,
or a selector that mentions a component, adds nothing to it: that class is the page's. This module also owns what
the components offer besides classes: which templates are shared components, and the variants each one knows.

Each test lists what it finds with where it is: fix it, or add an exception with the reason no component or
utility fits (guards.fail_with: fnmatch patterns over "where:name", and an exception matching nothing fails).

Checked: the tracked templates except the shared components themselves and standalone documents (an <html> of
their own; they carry their own <style> and don't load app.css). A class counts as used where a template writes
it (class attributes, cotton class props, x-transition), where a script on the page applies it (classList calls)
or names it (any string literal that is an app.css class, including a static/js file the page loads), and where
Python sets it in a "class" dict entry.
"""

import re
from collections.abc import Iterable
from functools import cache
from pathlib import Path

import django
from django.apps import apps
from django.contrib.messages import constants as message_constants

from core import status_variants
from core.admin_views import event_panel
from core.tests.guards import (
    REPO_DIR,
    TEMPLATES_DIR,
    WEB_DIR,
    fail_with,
    is_standalone_document,
    template_name,
    tracked_files,
    tracked_templates,
)

COTTON_DIR = TEMPLATES_DIR / "cotton"
APP_CSS = WEB_DIR / "static/css/app.css"
ADMIN_CSS = Path(django.__file__).parent / "contrib/admin/static/admin/css"
UTILITIES_RE = re.compile(r"/\* BEGIN UTILITIES \*/(.*?)/\* END UTILITIES \*/", re.DOTALL)

# The shared components: the portal's building blocks. Every .html in cotton/ is here or in PAGE_PARTIALS.
SHARED_COMPONENTS = {
    "action_box.html",
    "alert.html",
    "badge.html",
    "button.html",
    "button_row.html",
    "detail_grid.html",
    "detail_row.html",
    "empty_state.html",
    "fieldset.html",
    "filter_field.html",
    "filter_toolbar.html",
    "form.html",
    "form_field.html",
    "image_grid.html",
    "info_box.html",
    "link.html",
    "messages.html",
    "module.html",
    "nav.html",
    "nav_item.html",
    "page_header.html",
    "pagination.html",
    "progress_bar.html",
    "score_value.html",
    "table.html",
    "table_header.html",
    "toast.html",
    "toggle_button.html",
}
# One page's markup kept in cotton/, not shared; checked like pages
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

# Markup a page writes by hand that a shared component renders: (element, class or None, component)
RAW_MARKUP = [
    ("button", None, "<c-button>"),
    ("table", None, "<c-table>"),
    ("span", "badge", "<c-badge>"),
    ("div", "module", "<c-module>"),
    ("div", "submit-row", "<c-button_row>"),
    ("div", "action-box", "<c-action_box>"),
    ("div", "info-box", "<c-info_box>"),
    ("div", "alert", "<c-alert>"),
    ("div", "progress-bar", "<c-progress_bar>"),
    ("div", "form-row", "<c-form_field>"),
    ("div", "filter-field", "<c-filter_field>"),
    ("dl", "detail-list", "<c-detail_grid>"),
]

# Filters that turn a stored status into a variant the component knows (core.status_variants)
VARIANT_FILTERS = {"badge": "badge_variant", "alert": "alert_variant"}


# ---------------------------------------------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------------------------------------------

CHROME = "The portal chrome every page is framed in: "
COMMENTS = "A ticket's comments: a thread of bordered notes, only on the ticket page"
HISTORY = "A ticket's history: a table on wide screens, cards on phones"
ATTACHMENTS = "Ticket attachment previews; c-image_grid is a fixed-column grid of bare images"
MODAL = "The Create IP Pool dialog, the portal's only modal"

# "where:class" for a class no stylesheet defines and no script reads
UNDEFINED_EXCEPTIONS: dict[str, str] = {}

# "template:class" for a class app.css styles outside the shared vocabulary: page-specific styling
PAGE_STYLE_EXCEPTIONS: dict[str, str] = {
    "admin/base_site.html:primary-navigation": CHROME + "the nav bar",
    "admin/base_site.html:nav-toggle": CHROME + "the phone menu button",
    "admin/base_site.html:nav-menu": CHROME + "the nav links and tools",
    "admin/base_site.html:nav-menu--open": CHROME + "the phone menu, open",
    "admin/base_site.html:nav-links": CHROME + "the section links",
    "admin/base_site.html:nav-spacer": CHROME + "the gap before the tools",
    "admin/base_site.html:nav-utils": CHROME + "the dark mode, sound and log out tools",
    "admin/base_site.html:nav-user": CHROME + "the signed-in name",
    "admin/base_site.html:current": CHROME + "the current section's link",
    "admin/base_site.html:notif-count": CHROME + "open tickets on the Tickets link",
    "admin/base_site.html:dark-mode-toggle": CHROME + "the dark mode button",
    "admin/base_site.html:light-icon": CHROME + "the dark mode button's sun",
    "admin/base_site.html:dark-icon": CHROME + "the dark mode button's moon",
    "admin/base_site.html:audio-toggle": CHROME + "the ticket sound button",
    "admin/base_site.html:ticket-toast": CHROME + "the new-ticket toast",
    "admin/base_site.html:toast-line1": CHROME + "the new-ticket toast's number and title",
    "admin/base_site.html:toast-line2": CHROME + "the new-ticket toast's category",
    "admin/base_site.html:toast-enter": CHROME + "the new-ticket toast's slide in",
    "admin/base_site.html:toast-enter-start": CHROME + "the new-ticket toast's slide in",
    "admin/base_site.html:toast-enter-end": CHROME + "the new-ticket toast's slide in and out",
    "admin/base_site.html:toast-leave": CHROME + "the new-ticket toast's slide out",
    "admin/broadcast.html:team-list": "The recipient list flows into 2 to 4 columns by width; no utility sets columns",
    "admin/category_form.html:checkbox-group": "Stacks the field checkboxes 8px apart; no utility stacks with that gap",
    "ticketing/comments_list.html:comment-list": COMMENTS,
    "ticketing/comments_list.html:comment-item": COMMENTS,
    "ticketing/comments_list.html:comment-header": COMMENTS,
    "ticketing/comments_list.html:comment-author": COMMENTS,
    "ticketing/comments_list.html:comment-time": COMMENTS,
    "ticketing/comments_list.html:comment-body": COMMENTS,
    "ticketing/history_list.html:history-table": HISTORY,
    "ticketing/history_list.html:history-cards": HISTORY,
    "ticketing/history_list.html:history-card": HISTORY,
    "ticketing/history_list.html:history-card-header": HISTORY,
    "ticketing/history_list.html:history-card-time": HISTORY,
    "ticketing/history_list.html:history-card-details": HISTORY,
    "ticket_detail.html:attachment-grid": ATTACHMENTS,
    "ticket_detail.html:attachment-item": ATTACHMENTS,
    "ticket_detail.html:attachment-preview": ATTACHMENTS,
    "ticket_detail.html:attachment-thumbnail": ATTACHMENTS,
    "ticket_detail.html:attachment-pdf": ATTACHMENTS,
    "ticket_detail.html:attachment-file": ATTACHMENTS,
    "ticket_detail.html:attachment-info": ATTACHMENTS,
    "ticket_detail.html:attachment-name": ATTACHMENTS,
    "ticket_detail.html:attachment-meta": ATTACHMENTS,
    "ticket_detail.html:pdf-icon": ATTACHMENTS,
    "ticket_detail.html:file-icon": ATTACHMENTS,
    "ticket_detail.html:playbook-steps": "The category playbook's numbered steps on the ticket page",
    "scoring/_scorecard_body.html:neighbors": "The scorecard's column of the teams ranked around this one",
    "scoring/_scorecard_body.html:neighbor-current": "The scorecard's own team in that column",
    "scoring/_scorecard_body.html:scorecard-insights": "The scorecard's notes under each section",
    "scoring/_scorecard_body.html:row-total": "The scorecard's total row",
    "scoring/scorecard.html:scorecard": "The scorecard's print layout (font size, no page breaks inside tables)",
    "scoring/leaderboard.html:show-schools": "Switches the leaderboard between team and school names",
    "scoring/leaderboard.html:team-name": "The leaderboard's team names, hidden while it shows schools",
    "scoring/leaderboard.html:school-name": "The leaderboard's school names, hidden while it shows teams",
    "scoring/match_incident.html:module-header": (
        "The match page's two panels carry a full-width title bar; c-module's heading is Django's compact bar"
    ),
    "scoring/match_incident.html:module-header-success": "The match page's matching panel: its title bar in green",
    "scoring/partials/inject_feedback_table.html:click-to-edit": "Feedback edited in place; no other table does",
    "scoring/scoring_config.html:max-points-grid": (
        "The weight and maximum fields side by side, wrapping below 200px each; grid-2 and grid-4 have fixed columns"
    ),
    "scoring/submit_incident.html:screenshot-preview-*": "Previews of the screenshots chosen for upload",
    "scoring/submit_red_finding.html:screenshot-preview-*": "Previews of the screenshots chosen for upload",
    "scoring/submit_red_finding.html:team-btn": "The team picker's buttons, outlined",
    "scoring/submit_red_finding.html:team-btn--selected": "A picked team's button, red",
    "scoring/submit_red_finding.html:modal": MODAL,
    "scoring/submit_red_finding.html:modal--open": MODAL,
    "scoring/submit_red_finding.html:modal-backdrop": MODAL,
    "scoring/submit_red_finding.html:modal-content": MODAL,
    "scoring/submit_red_finding.html:modal-header": MODAL,
    "scoring/submit_red_finding.html:modal-title": MODAL,
    "scoring/submit_red_finding.html:modal-close": MODAL,
    "scoring/submit_red_finding.html:modal-body": MODAL,
    "scoring/submit_red_finding.html:modal-footer": MODAL,
}

# "template:<c-component>" for markup written by hand where a shared component renders it
RAW_MARKUP_EXCEPTIONS: dict[str, str] = {
    "admin/base_site.html:<c-button>": (
        "The chrome's menu, dark mode and sound toggles; c-button would put Django's filled .button on the nav bar"
    ),
    "admin/index.html:<c-module>": "Django admin's own index page: its recent actions box, kept as Django writes it",
    "orange_team/check_form.html:<c-form_field>": (
        "Rubric rows Alpine repeats (x-for); c-form_field takes a fixed label and id, these are bound per row"
    ),
    "scoring/email_scorecards_confirm.html:<c-alert>": (
        "The send result shown in place, its class bound by Alpine; c-alert's variant is fixed at render and "
        "c-toast floats in the corner"
    ),
    "scoring/submit_red_finding.html:<c-button>": (
        "The team picker and the dialog's close button have their own look; c-button would add Django's filled .button"
    ),
}

# "template:style" for a style attribute or <style> block on a page
INLINE_STYLE_EXCEPTIONS: dict[str, str] = {}

# '<template>:<c-name variant="...">' for a variant the component doesn't compare against
VARIANT_EXCEPTIONS: dict[str, str] = {
    'admin/includes/readiness_check.html:<c-badge variant="">': (
        "badge.html: an empty variant leaves the badge--* class to the row's Alpine ::class binding"
    ),
    'admin/competition.html:<c-badge variant="{{ panel.status_badge }}">': (
        "event_panel.STATUS_SHOWN maps each status to a variant; test_every_stored_status_maps_to_a_known_variant "
        "checks it"
    ),
    'admin/competition.html:<c-badge variant="{{ row.badge }}">': (
        "event_panel.STATE_SHOWN maps each step state to a variant; test_every_stored_status_maps_to_a_known_variant "
        "checks it"
    ),
}

# "app.css:class" for a class app.css defines that no template, script or Python source puts on a page
UNUSED_CSS_EXCEPTIONS: dict[str, str] = {
    "app.css:htmx-request": "htmx adds it to an element while its request runs",
    "app.css:dashboard": "Django admin's index page sets it on <body> (its bodyclass block); app.css pads that page",
}


# ---------------------------------------------------------------------------------------------------------------
# Reading templates, scripts and stylesheets
# ---------------------------------------------------------------------------------------------------------------

COMMENT_RE = re.compile(r"{#.*?#}|{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.DOTALL)
TEMPLATE_TAG_RE = re.compile(r"{%.*?%}|{{.*?}}", re.DOTALL)
PLACEHOLDER_RE = re.compile("\x01(\\d+)\x02")
TAG_RE = re.compile(
    r"""<(?P<name>[A-Za-z][\w:.-]*)(?=[\s/>])(?P<attrs>(?:\s*[^\s=>/"']+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+))?)*)\s*/?>"""
)
ATTR_RE = re.compile(r"""([^\s=>/"']+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>"']+))?""")
SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
STYLE_BLOCK_RE = re.compile(r"<style\b", re.IGNORECASE)
STATIC_JS_RE = re.compile(r"""{%\s*static\s+["'](js/[\w.-]+\.js)["']\s*%}""")
CLASS_NAME_RE = re.compile(r"-?[A-Za-z_][\w-]*")
CYCLE_RE = re.compile(r"{%\s*cycle\b(.*?)%}", re.DOTALL)
QUOTED_RE = re.compile(r"""(["'])(.*?)\1""")
STRING_LITERAL_RE = re.compile(r"""(["'`])((?:\\.|(?!\1)[^\\\n])*)\1""")
CLASSLIST_RE = re.compile(
    r"""classList\.(?:add|remove|toggle|replace)\(\s*(["'])([^"']+)\1(?:\s*,\s*(["'])([^"']+)\3)?"""
)
SELECTOR_CALL_RE = re.compile(r"""(?:querySelector(?:All)?|closest|matches)\(\s*(["'`])([^"'`]+)\1""")
DICT_CLASS_RE = re.compile(r"""["']class["']\s*:\s*["']([^"']+)["']""")
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
CSS_RULE_RE = re.compile(r"([^{}]+)\{[^{}]*\}")
CSS_CLASS_RE = re.compile(r"\.(-?[A-Za-z_][\w-]*)")
TRANSITION_ATTRS = {f"x-transition:{phase}{part}" for phase in ("enter", "leave") for part in ("", "-start", "-end")}
STYLE_ATTRS = {"style", ":style", "::style", "x-bind:style"}


class Tag:
    """An element or component tag, with template tags in its attribute values kept as written."""

    def __init__(self, name: str, attrs: dict[str, str], line: int) -> None:
        self.name = name
        self.attrs = attrs
        self.line = line


@cache
def text(path: Path) -> str:
    """The file without template comments, keeping their line breaks so line numbers still match."""
    return COMMENT_RE.sub(lambda m: "\n" * m.group().count("\n"), path.read_text(encoding="utf-8"))


@cache
def tags(path: Path) -> tuple[Tag, ...]:
    """Every tag in the template. Template tags are set aside first, so a ">" or quote inside one ({% if n > 0 %})
    neither ends the tag nor its attribute."""
    content = text(path)
    kept: list[str] = []

    def hide(match: re.Match[str]) -> str:
        kept.append(match.group())
        return f"\x01{len(kept) - 1}\x02"

    hidden = TEMPLATE_TAG_RE.sub(hide, content)

    def restore(value: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: kept[int(m.group(1))], value)

    found = []
    for match in TAG_RE.finditer(hidden):
        attrs = {}
        for name, value in ATTR_RE.findall(match.group("attrs")):
            # An attribute inside {% if %}...{% endif %} reads as the attribute itself
            name = PLACEHOLDER_RE.sub("", name)
            if name:
                attrs[name] = restore(value[1:-1] if value[:1] in "\"'" else value)
        found.append(Tag(match.group("name").lower(), attrs, hidden.count("\n", 0, match.start()) + 1))
    return tuple(found)


def literal_classes(literal: str) -> set[str]:
    return {token for token in literal.split() if CLASS_NAME_RE.fullmatch(token)}


def attribute_classes(value: str) -> set[str]:
    """The class names in a class attribute. {% %} tags separate names and a name containing {{ }} is skipped, as it
    is only known at render; a {% cycle %} writes its quoted arguments."""
    cycled = {
        name for tag in CYCLE_RE.findall(value) for _, arg in QUOTED_RE.findall(tag) for name in literal_classes(arg)
    }
    value = re.sub(r"{%.*?%}", " ", value, flags=re.DOTALL)
    value = re.sub(r"{{.*?}}", "\0", value, flags=re.DOTALL)
    return cycled | {token for token in value.split() if "\0" not in token and CLASS_NAME_RE.fullmatch(token)}


def written_classes(path: Path) -> set[str]:
    """Classes the template writes: class attributes (on elements and on components) and x-transition classes."""
    names: set[str] = set()
    for tag in tags(path):
        for attr, value in tag.attrs.items():
            if attr == "class" or attr in TRANSITION_ATTRS:
                names |= attribute_classes(value)
    return names


@cache
def static_scripts() -> dict[str, Path]:
    """static/js files by the path templates load them with (js/utils.js)."""
    return {
        p.removeprefix("web/static/"): REPO_DIR / p
        for p in tracked_files()
        if p.startswith("web/static/js/") and p.endswith(".js")
    }


def script_texts(path: Path) -> list[str]:
    """The script a template runs: its <script> blocks, its on* handlers, and the static/js files it loads."""
    content = text(path)
    found = SCRIPT_RE.findall(content)
    found += [value for tag in tags(path) for attr, value in tag.attrs.items() if attr.startswith("on")]
    loaded = static_scripts()
    found += [loaded[name].read_text(encoding="utf-8") for name in STATIC_JS_RE.findall(content) if name in loaded]
    return found


def scripted_classes(path: Path) -> set[str]:
    """Classes the template's script applies with classList, and the string literals made only of stylesheet
    classes (a getter's return value, a lookup table's entry)."""
    found: set[str] = set()
    for script in script_texts(path):
        for m in CLASSLIST_RE.finditer(script):
            found |= literal_classes(m.group(2)) | literal_classes(m.group(4) or "")
        for m in STRING_LITERAL_RE.finditer(script):
            words = m.group(2).split()
            if words and set(words) <= app_css_classes() | admin_css_classes():
                found |= set(words)
    return found


@cache
def shared_component_paths() -> tuple[Path, ...]:
    return tuple(p for p in tracked_templates() if p.parent == COTTON_DIR and p.name in SHARED_COMPONENTS)


@cache
def checked_templates() -> tuple[Path, ...]:
    """Pages and page partials: not the shared components, not standalone documents."""
    shared = set(shared_component_paths())
    return tuple(p for p in tracked_templates() if p not in shared and not is_standalone_document(p))


@cache
def python_sources() -> tuple[Path, ...]:
    not_source = re.compile(r"/(tests|migrations|browser_tests|integration_tests)/|/test_[^/]*\.py$|/conftest\.py$")
    return tuple(
        REPO_DIR / p for p in tracked_files() if p.startswith("web/") and p.endswith(".py") and not not_source.search(p)
    )


def add(found: dict[str, set[str]], where: str, names: Iterable[str]) -> None:
    for name in names:
        found.setdefault(name, set()).add(where)


@cache
def page_uses() -> dict[str, set[str]]:
    """Class -> the pages (and Python files) that put it on a page."""
    found: dict[str, set[str]] = {}
    for path in checked_templates():
        add(found, template_name(path), written_classes(path) | scripted_classes(path))
    for path in python_sources():
        add(
            found,
            path.relative_to(WEB_DIR).as_posix(),
            (n for v in DICT_CLASS_RE.findall(path.read_text()) for n in literal_classes(v)),
        )
    return found


@cache
def component_uses() -> dict[str, set[str]]:
    """Class -> the shared components that render it."""
    found: dict[str, set[str]] = {}
    for path in shared_component_paths():
        add(found, template_name(path), written_classes(path))
    return found


@cache
def hooks() -> frozenset[str]:
    """Classes the portal's script reads to find elements (querySelector('.box-toggle')). Such a class counts as
    defined even with no rule of its own."""
    portal = [p for p in tracked_templates() if not is_standalone_document(p)]
    return frozenset(
        name
        for path in portal
        for script in script_texts(path)
        for m in SELECTOR_CALL_RE.finditer(script)
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
    """The classes the shared components render, plus app.css's modifiers of them (alert--error,
    progress-bar__fill--warn): the component's own family, whatever selector styles it."""
    rendered = set(component_uses())
    family = {name for name in app_css_classes() for root in rendered if name.startswith((f"{root}--", f"{root}__"))}
    return frozenset(rendered | family)


@cache
def known_variants() -> dict[str, set[str]]:
    """Component -> the variants its template compares `variant` with, plus its default."""
    known = {}
    for path in shared_component_paths():
        content = path.read_text()
        compared = set(re.findall(r"variant\s*==\s*['\"]([^'\"]*)['\"]", content))
        if compared:
            default = re.search(r'<c-vars\b[^>]*\bvariant="([^"]*)"', content)
            known[path.stem] = compared | ({default.group(1)} if default else set())
    return known


def by_use(uses: dict[str, set[str]], names: Iterable[str]) -> dict[str, str]:
    return {f"{where}:{name}": "" for name in names for where in uses[name]}


# ---------------------------------------------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------------------------------------------


def test_all_components_registered() -> None:
    """Every component is either shared (part of the vocabulary) or one page's partial (checked like a page)."""
    actual = {p.name for p in tracked_templates() if p.parent == COTTON_DIR}
    registered = SHARED_COMPONENTS | PAGE_PARTIALS
    unknown = sorted(actual - registered)
    deleted = sorted(registered - actual)
    assert not unknown, f"Add these cotton files to SHARED_COMPONENTS or PAGE_PARTIALS: {unknown}"
    assert not deleted, f"These are in SHARED_COMPONENTS or PAGE_PARTIALS but not in cotton/: {deleted}"


def test_utilities_section_is_marked() -> None:
    assert len(UTILITIES_RE.findall(APP_CSS.read_text())) == 1, (
        "app.css needs one BEGIN UTILITIES/END UTILITIES section"
    )


def test_every_class_is_defined() -> None:
    """A class no stylesheet defines does nothing (no Bootstrap is loaded), unless script reads it as a hook."""
    defined = app_css_classes() | admin_css_classes() | hooks()
    found = by_use(page_uses(), set(page_uses()) - defined) | by_use(component_uses(), set(component_uses()) - defined)
    fail_with(
        "Classes no stylesheet defines: define it in app.css, use a component or utility, or drop it",
        found,
        UNDEFINED_EXCEPTIONS,
    )


def test_classes_come_from_the_shared_vocabulary() -> None:
    """app.css classes a page uses come from a shared component or the utilities, not a style the page invented."""
    vocabulary = component_classes() | utility_classes() | admin_css_classes()
    fail_with(
        "Page-specific styling: use a cotton component or an app.css utility, or add an exception saying why none fits",
        by_use(page_uses(), (set(page_uses()) & app_css_classes()) - vocabulary),
        PAGE_STYLE_EXCEPTIONS,
    )


def test_no_hand_written_component_markup() -> None:
    lines: dict[str, list[str]] = {}
    for path in checked_templates():
        for tag in tags(path):
            classes = attribute_classes(tag.attrs.get("class", ""))
            for element, cls, component in RAW_MARKUP:
                if tag.name == element and (cls is None or cls in classes):
                    lines.setdefault(f"{template_name(path)}:{component}", []).append(str(tag.line))
    found = {key: f"line {', '.join(ln)}" for key, ln in lines.items()}
    fail_with("Markup a shared component renders, written by hand", found, RAW_MARKUP_EXCEPTIONS)


def test_no_inline_styles() -> None:
    """A page's look comes from classes: a style attribute or <style> block is styling the vocabulary can't see."""
    lines: dict[str, list[str]] = {}
    for path in checked_templates():
        where = f"{template_name(path)}:style"
        for tag in tags(path):
            if STYLE_ATTRS & set(tag.attrs):
                lines.setdefault(where, []).append(str(tag.line))
        for m in STYLE_BLOCK_RE.finditer(text(path)):
            lines.setdefault(where, []).append(f"<style> at {text(path).count(chr(10), 0, m.start()) + 1}")
    found = {key: f"line {', '.join(ln)}" for key, ln in lines.items()}
    fail_with("Inline styles: use a utility or a component option", found, INLINE_STYLE_EXCEPTIONS)


def test_variants_are_ones_the_component_knows() -> None:
    """A variant the component doesn't know falls back to its default look without an error (an alert with
    variant="danger" rendered green). A variant from a stored status goes through the status's mapping."""
    known = known_variants()
    found: dict[str, str] = {}
    for path in tracked_templates():
        if is_standalone_document(path):
            continue
        for tag in tags(path):
            name = tag.name.removeprefix("c-")
            if not tag.name.startswith("c-") or name not in known or "variant" not in tag.attrs:
                continue
            variant = tag.attrs["variant"]
            key = f'{template_name(path)}:<c-{name} variant="{variant}">'
            if "{{" in variant:
                if f"|{VARIANT_FILTERS.get(name)}" not in variant.replace(" ", ""):
                    found[key] = f"line {tag.line}: map the status with |{VARIANT_FILTERS.get(name, '...')}"
            elif variant not in known[name]:
                found[key] = f"line {tag.line}: known: {', '.join(sorted(known[name]))}"
    fail_with("Unknown component variant", found, VARIANT_EXCEPTIONS)


def test_every_stored_status_maps_to_a_known_variant() -> None:
    """Each status kind covers every stored value, with a variant the component knows."""
    known = known_variants()
    problems = []
    for kind, (label, field_name, mapping) in status_variants.BADGE_STATUSES.items():
        choices = {value for value, _ in apps.get_model(label)._meta.get_field(field_name).choices}
        if set(mapping) != choices:
            problems.append(f"{kind}: maps {sorted(mapping)}, {label}.{field_name} stores {sorted(choices)}")
        problems += [
            f"{kind}: {v} -> badge {variant}" for v, variant in mapping.items() if variant not in known["badge"]
        ]
    tags_shown = set(message_constants.DEFAULT_TAGS.values())
    if set(status_variants.MESSAGE_ALERTS) != tags_shown:
        problems.append(f"messages: maps {sorted(status_variants.MESSAGE_ALERTS)}, Django tags {sorted(tags_shown)}")
    problems += [
        f"messages: {t} -> alert {v}" for t, v in status_variants.MESSAGE_ALERTS.items() if v not in known["alert"]
    ]
    for table in (event_panel.STATE_SHOWN, event_panel.STATUS_SHOWN):
        problems += [f"event panel: {state} -> badge {v}" for state, (_, v) in table.items() if v not in known["badge"]]
    assert not problems, "\n".join(problems)


def test_every_app_css_class_is_used() -> None:
    """A rule no page or component puts on an element is dead weight that misleads the next reader of app.css."""
    used = set(page_uses()) | set(component_uses())
    fail_with(
        "app.css classes no template, script or Python source puts on a page: remove the rule",
        {f"app.css:{name}": "" for name in app_css_classes() - used},
        UNUSED_CSS_EXCEPTIONS,
    )
