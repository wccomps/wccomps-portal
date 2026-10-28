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
    Tag,
    elements,
    fail_with,
    page_templates,
    portal_templates,
    scripts,
    static_scripts,
    tags,
    template_name,
    text,
    tracked_files,
    tracked_templates,
    with_open_elements,
)

COTTON_DIR = TEMPLATES_DIR / "cotton"
APP_CSS = WEB_DIR / "static/css/app.css"
ADMIN_CSS = Path(django.__file__).parent / "contrib/admin/static/admin/css"
UTILITIES_RE = re.compile(r"/\* BEGIN UTILITIES \*/(.*?)/\* END UTILITIES \*/", re.DOTALL)

# The shared components: the portal's building blocks. Every .html in cotton/ is here.
SHARED_COMPONENTS = {
    "action_box.html",
    "alert.html",
    "approval_filter.html",
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
    "form_errors.html",
    "form_field.html",
    "image_grid.html",
    "info_box.html",
    "link.html",
    "messages.html",
    "module.html",
    "no_matches.html",
    "nav.html",
    "nav_item.html",
    "page_header.html",
    "pagination.html",
    "progress_bar.html",
    "score_value.html",
    "table.html",
    "table_header.html",
    "team_filter.html",
    "toast.html",
    "toggle_button.html",
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
    ("fieldset", None, "<c-fieldset>"),
    ("form", None, "<c-form>"),
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
    "admin/base_site.html:nav-icon": CHROME + "the menu, dark mode and sound buttons' icons",
    "admin/base_site.html:nav-menu": CHROME + "the nav links and tools",
    "admin/base_site.html:nav-menu--open": CHROME + "the phone menu, open",
    "admin/base_site.html:nav-links": CHROME + "the section links",
    "admin/base_site.html:nav-spacer": CHROME + "the gap before the tools",
    "admin/base_site.html:nav-utils": CHROME + "the dark mode, sound and log out tools",
    "admin/base_site.html:nav-user": CHROME + "the signed-in name",
    "admin/base_site.html:current": CHROME + "the current section's link",
    "admin/base_site.html:notif-count": CHROME + "open tickets on the Tickets link",
    "admin/base_site.html:dark-mode-toggle": CHROME + "the dark mode button",
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
    "scoring/submit_incident.html:screenshot-preview-img": "A chosen screenshot's preview image",
    "scoring/submit_incident.html:screenshot-preview-name": "A chosen screenshot's file name",
    "scoring/submit_red_finding.html:screenshot-preview-img": "A chosen screenshot's preview image",
    "scoring/submit_red_finding.html:screenshot-preview-name": "A chosen screenshot's file name",
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
    "forms/field.html:*": "Django's field group template: Django renders it, outside cotton, as the form row markup",
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

STYLE_BLOCK_RE = re.compile(r"<style\b", re.IGNORECASE)
STATIC_JS_RE = re.compile(r"""{%\s*static\s+["'](js/[\w.-]+\.js)["']\s*%}""")
CLASS_NAME_RE = re.compile(r"-?[A-Za-z_][\w-]*")
CYCLE_RE = re.compile(r"{%\s*cycle\b(.*?)%}", re.DOTALL)
QUOTED_RE = re.compile(r"""(["'])(.*?)\1""")
STRING_LITERAL_RE = re.compile(r"""(["'`])((?:\\.|(?!\1)[^\\\n])*)\1""")
BINDING_ATTRS = {":class", "::class", "x-bind:class"}
PROPERTY_PATH_RE = re.compile(r"[\w$]+(?:\.[\w$]+)*")
# A string a getter returns, or a ternary branch or property value yields
RESULT_LITERAL_RE = re.compile(r"""(?:return|\?|:|\|\|)\s*(["'])([^"']*)\1""")
LENGTH_RE = re.compile(r"\d+(?:\.\d+)?(?:px|%|rem|ch)|auto")
VARIANT_FILTER_RE = re.compile(r"""^{{\s*(?P<expr>[\w.]+)\s*\|\s*badge_variant\s*:\s*(["'])(?P<kind>\w+)\2\s*}}$""")
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
def loaded_scripts() -> dict[str, str]:
    """static/js files' JavaScript by the path templates load them with (js/utils.js)."""
    return {name.removeprefix("web/static/"): js for name, js in static_scripts()}


def script_texts(path: Path) -> list[str]:
    """The script a template runs: its <script> blocks, its on* handlers, and the static/js files it loads."""
    found = [script for _, script in scripts(path)]
    found += [value for tag in tags(path) for attr, value in tag.attrs.items() if attr.startswith("on")]
    loaded = loaded_scripts()
    found += [loaded[name] for name in STATIC_JS_RE.findall(text(path)) if name in loaded]
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


def bound_names(path: Path) -> set[str]:
    """The properties the template's :class bindings read (r.rowClass reads rowClass)."""
    return {
        value.split(".")[-1]
        for tag in tags(path)
        for attr, value in tag.attrs.items()
        if attr in BINDING_ATTRS and PROPERTY_PATH_RE.fullmatch(value)
    }


@cache
def portal_scripts() -> tuple[str, ...]:
    """Every script the portal runs: the non-standalone templates' scripts and static/js."""
    found = [script for p in portal_templates() for _, script in scripts(p)]
    return tuple(found + [js for _, js in static_scripts()])


@cache
def getter_classes(name: str) -> frozenset[str]:
    """The class names a getter or property called `name` can return, in any of the portal's scripts."""
    found: set[str] = set()
    for script in portal_scripts():
        bodies = re.findall(rf"\bget\s+{re.escape(name)}\s*\(\)\s*\{{(.*?)\}}", script, re.DOTALL)
        bodies += [":" + value for value in re.findall(rf"\b{re.escape(name)}\s*:\s*([^,\n]+)", script)]
        for body in bodies:
            found |= {c for m in RESULT_LITERAL_RE.finditer(body) for c in literal_classes(m.group(2))}
    return frozenset(found)


def bound_classes(path: Path) -> set[str]:
    """Classes the template's :class bindings can apply, read from the getters they name."""
    return {c for name in bound_names(path) for c in getter_classes(name)}


@cache
def shared_component_paths() -> tuple[Path, ...]:
    return tuple(p for p in tracked_templates() if p.parent == COTTON_DIR and p.name in SHARED_COMPONENTS)


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
    for path in page_templates():
        add(found, template_name(path), written_classes(path) | scripted_classes(path))
    for path in python_sources():
        add(
            found,
            path.relative_to(WEB_DIR).as_posix(),
            (n for v in DICT_CLASS_RE.findall(text(path)) for n in literal_classes(v)),
        )
    return found


@cache
def component_uses() -> dict[str, set[str]]:
    """Class -> the shared components that render it, in their markup or through the getters their :class
    bindings read (fieldset's iconClass)."""
    found: dict[str, set[str]] = {}
    for path in shared_component_paths():
        add(found, template_name(path), written_classes(path) | bound_classes(path))
    return found


@cache
def bound_page_uses() -> dict[str, set[str]]:
    """Class -> the pages whose :class bindings can apply it. Only the undefined-class check reads this: a getter
    returning a mistyped class (alert--sucess) renders an unstyled element."""
    found: dict[str, set[str]] = {}
    for path in page_templates():
        add(found, template_name(path), bound_classes(path))
    return found


@cache
def hooks() -> frozenset[str]:
    """Classes the portal's script reads to find elements (querySelector('.box-toggle')). Such a class counts as
    defined even with no rule of its own."""
    portal = portal_templates()
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
    """The classes the shared components render. A modifier counts only when a component renders it too: a page
    can't add .alert--purple to app.css and call it the alert's."""
    return frozenset(component_uses())


@cache
def known_variants() -> dict[str, set[str]]:
    """Component -> the variants its template compares `variant` with, plus its default."""
    known = {}
    for path in shared_component_paths():
        content = text(path)
        compared = set(re.findall(r"variant\s*==\s*['\"]([^'\"]*)['\"]", content))
        if compared:
            defaults = {tag.attrs["variant"] for tag in tags(path) if tag.name == "c-vars" and "variant" in tag.attrs}
            known[path.stem] = compared | defaults
    return known


def by_use(uses: dict[str, set[str]], names: Iterable[str]) -> dict[str, str]:
    return {f"{where}:{name}": "" for name in names for where in uses[name]}


# ---------------------------------------------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------------------------------------------


def test_all_components_registered() -> None:
    """Every component is a shared one (part of the vocabulary); one page's markup is an include beside the page."""
    actual = {p.name for p in tracked_templates() if p.parent == COTTON_DIR}
    unknown = sorted(actual - SHARED_COMPONENTS)
    deleted = sorted(SHARED_COMPONENTS - actual)
    assert not unknown, f"Add these cotton files to SHARED_COMPONENTS: {unknown}"
    assert not deleted, f"These are in SHARED_COMPONENTS but not in cotton/: {deleted}"


def test_utilities_section_is_marked() -> None:
    assert len(UTILITIES_RE.findall(APP_CSS.read_text())) == 1, (
        "app.css needs one BEGIN UTILITIES/END UTILITIES section"
    )


def test_every_class_is_defined() -> None:
    """A class no stylesheet defines does nothing (no Bootstrap is loaded), unless script reads it as a hook."""
    defined = app_css_classes() | admin_css_classes() | hooks()
    found = (
        by_use(page_uses(), set(page_uses()) - defined)
        | by_use(bound_page_uses(), set(bound_page_uses()) - defined)
        | by_use(component_uses(), set(component_uses()) - defined)
    )
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


def get_form(tag: Tag, component: str) -> bool:
    """A GET form (a filter toolbar): c-form is a POST form."""
    return component == "<c-form>" and tag.attrs.get("method", "get").lower() != "post"


def test_no_hand_written_component_markup() -> None:
    lines: dict[str, list[str]] = {}
    for path in page_templates():
        for tag in tags(path):
            classes = attribute_classes(tag.attrs.get("class", ""))
            for element, cls, component in RAW_MARKUP:
                if tag.name == element and (cls is None or cls in classes) and not get_form(tag, component):
                    lines.setdefault(f"{template_name(path)}:{component}", []).append(str(tag.line))
    found = {key: f"line {', '.join(ln)}" for key, ln in lines.items()}
    fail_with("Markup a shared component renders, written by hand", found, RAW_MARKUP_EXCEPTIONS)


def test_button_rows_end_a_section() -> None:
    """A button row is a row of buttons that ends a section (cotton README, <c-button_row>); inside a detail grid it
    sits among the label/value rows. Put it after the grid."""
    found = {
        f"{template_name(path)}:{tag.line}": "a c-button_row inside a c-detail_grid"
        for path in page_templates()
        for tag, open_tags in with_open_elements(path)
        if tag.name == "c-button_row" and any(t.name in ("c-detail_grid", "c-detail_row") for t in open_tags)
    }
    fail_with("Button rows inside a detail grid", found, {})


BUTTON_ROW_LINK_VARIANTS = {"primary", "secondary", "danger", "warning", "cancel"}
BOXED_BUTTON_ROW_EXCEPTIONS = {
    "admin/team_detail.html": "The multi-action team actions bar (a row that is the whole section)",
}
STANDALONE_HEADER_EXCEPTIONS = {
    "core/oauth_error.html": "Standalone OAuth error page outside the section hierarchy",
    "error.html": "Central error page outside the section hierarchy",
    "link_confirm.html": "Discord link token flow page outside the section hierarchy",
    "link_error.html": "Discord link token flow page outside the section hierarchy",
    "link_success.html": "Discord link token flow page outside the section hierarchy",
    "registration/edit.html": "Public token-authenticated school registration edit page",
    "registration/edit_locked.html": "Public token-authenticated school registration locked page",
}
URL_TAG_NAME_RE = re.compile(r"""{%\s*url\s+["']([^"']+)["']""")
VAR_PLACEHOLDER_RE = re.compile(r"{{.*?}}")


def test_button_row_links_have_a_button_variant() -> None:
    """c-link's default variant renders a plain text link; inside a c-button_row or .field-actions cell every link is
    an action button."""
    found = {
        f"{template_name(path)}:{tag.line}": f"variant={tag.attrs.get('variant', 'default')!r}"
        for path in page_templates()
        for tag, open_tags in with_open_elements(path)
        if tag.name == "c-link"
        and any(
            t.name == "c-button_row" or "field-actions" in attribute_classes(t.attrs.get("class", ""))
            for t in open_tags
        )
        and tag.attrs.get("variant") not in BUTTON_ROW_LINK_VARIANTS
    }
    fail_with(
        "c-link in a c-button_row or .field-actions without a button variant "
        f"({', '.join(sorted(BUTTON_ROW_LINK_VARIANTS))})",
        found,
        {},
    )


def test_boxed_button_rows_are_action_toolbars() -> None:
    """c-button_row's boxed grey background is only for standalone action bars, not regular form submit rows."""
    found = {
        template_name(path): f"line {tag.line}"
        for path in page_templates()
        for tag in tags(path)
        if tag.name == "c-button_row" and tag.attrs.get("boxed") == "true"
    }
    fail_with("Templates using c-button_row boxed='true'", found, BOXED_BUTTON_ROW_EXCEPTIONS)


FIELDSET_PADDED_TAGS = {
    "c-action_box",
    "c-alert",
    "c-button_row",
    "c-detail_grid",
    "c-empty_state",
    "c-form_field",
    "c-image_grid",
    "c-info_box",
    "c-progress_bar",
    "c-slot",
    "c-table",
    "c-toast",
    "dl",
    "h2",
    "h3",
    "h4",
    "input",
    "p",
    "pre",
    "script",
    "template",
    "ul",
}
FIELDSET_PADDED_CLASSES = {
    "code-block",
    "form-row",
    "p-10",
    "p-15",
    "p-20",
    "playbook-steps",
    "px-10",
    "status-bar",
}


def wraps_a_field_group(path: Path, start: Tag, lines: list[str]) -> bool:
    """Whether the element opened by start holds a {{ form.x.as_field_group }}, which renders a padded .form-row."""
    depth = 0
    for tag in elements(path):
        if tag.line < start.line or tag.name != start.name:
            continue
        depth += -1 if tag.closing else 1
        if depth == 0:
            return any("as_field_group" in line for line in lines[start.line - 1 : tag.line])
    return False


def test_fieldset_children_are_padded() -> None:
    """Django admin's base.css sets padding: 0 on fieldset.module and only pads p, ul, h3, h4, dl, pre and .form-row.
    Any direct child of <c-fieldset> must be one of those, carry a padding class, or wrap padded children."""
    found: dict[str, str] = {}
    for path in page_templates():
        with_parents = list(with_open_elements(path))
        # Direct children of <c-fieldset> that themselves wrap a padded component (c-form_field, c-table)
        wrappers_with_padded_child = {
            open_tags[i + 1].line
            for tag, open_tags in with_parents
            for i, ancestor in enumerate(open_tags[:-1])
            if ancestor.name == "c-fieldset" and tag.name in ("c-form_field", "c-table")
        }
        lines = text(path).splitlines()
        for tag, open_tags in with_parents:
            if not open_tags or open_tags[-1].name != "c-fieldset":
                continue
            classes = attribute_classes(tag.attrs.get("class", ""))
            if (
                tag.name in FIELDSET_PADDED_TAGS
                or classes & FIELDSET_PADDED_CLASSES
                or tag.line in wrappers_with_padded_child
                or "hx-get" in tag.attrs
                or wraps_a_field_group(path, tag, lines)
            ):
                continue
            found[f"{template_name(path)}:{tag.line}"] = f"<{tag.name} class={tag.attrs.get('class', '')!r}>"
    fail_with("Unpadded direct child of <c-fieldset> (add p-10/px-10 or use <c-form_field>)", found, {})


def test_form_field_id_matches_child_control() -> None:
    """When <c-form_field> or <c-filter_field> wraps explicit HTML controls rather than a Django {{ form.field }}
    widget, id="X" must match a child control's id="X", and any unwrapped <input>/<select>/<textarea> requires id."""
    found: dict[str, str] = {}
    for path in page_templates():
        with_parents = list(with_open_elements(path))
        for tag, _ in with_parents:
            if tag.name not in ("c-form_field", "c-filter_field"):
                continue
            descendants = [
                (t, ancestors)
                for t, ancestors in with_parents
                if any(a.line == tag.line and a.name == tag.name for a in ancestors)
            ]
            if "id" in tag.attrs:
                fid = tag.attrs["id"]
                if descendants and not any(d.attrs.get("id") == fid or ":id" in d.attrs for d, _ in descendants):
                    found[f"{template_name(path)}:{tag.line}"] = f"<{tag.name} id={fid!r}> has no child with id={fid!r}"
            elif ":id" not in tag.attrs:
                unlabeled = [
                    d.name
                    for d, ancestors in descendants
                    if d.name in ("input", "select", "textarea")
                    and not any(a.name == "label" for a in ancestors)
                    and "aria-label" not in d.attrs
                ]
                if unlabeled:
                    found[f"{template_name(path)}:{tag.line}"] = f"<{tag.name}> missing id for child {unlabeled[0]}"
    fail_with("Broken or missing <label for=...> on <c-form_field> or <c-filter_field>", found, {})


def test_page_header_hierarchy() -> None:
    """Nav and sub-nav root pages have no parent_url or page-level Cancel link; child pages have both parent_url and
    parent_label, and every child of the same parent URL uses the same parent_label."""
    from core.tests.test_context_processors import _url_templates

    tpl_to_urls: dict[str, set[str]] = {}
    for url_name, tpls in _url_templates().items():
        for tpl in tpls:
            tpl_to_urls.setdefault(tpl, set()).add(url_name)

    base_site = text(TEMPLATES_DIR / "admin/base_site.html")
    nav_links_match = re.search(r'<div class="nav-links">(.*?)</div>', base_site, re.DOTALL)
    nav_links_html = nav_links_match.group(1) if nav_links_match else ""
    nav_root_urls = {u.split(":")[-1] for u in URL_TAG_NAME_RE.findall(nav_links_html)}
    for path in page_templates():
        for tag in tags(path):
            if tag.name == "c-nav_item":
                nav_root_urls.update(u.split(":")[-1] for u in URL_TAG_NAME_RE.findall(tag.attrs.get("href", "")))

    problems: dict[str, str] = {}
    labels_by_parent: dict[str, dict[str, list[str]]] = {}

    for path in page_templates():
        name = template_name(path)
        headers = [t for t in tags(path) if t.name == "c-page_header"]
        if not headers:
            continue
        is_root = bool(tpl_to_urls.get(name, set()) & nav_root_urls)
        cancel_links = [t for t in tags(path) if t.name == "c-link" and t.attrs.get("variant") == "cancel"]
        for header in headers:
            has_url = bool(header.attrs.get("parent_url"))
            has_label = bool(header.attrs.get("parent_label"))
            if is_root:
                if has_url or has_label:
                    problems[name] = f"line {header.line}: nav root page has parent_url/parent_label"
                if cancel_links:
                    problems[name] = f"line {cancel_links[0].line}: nav root page has a Cancel link"
            elif not (has_url and has_label):
                problems[name] = f"line {header.line}: child page missing parent_url or parent_label"
            if has_url and has_label:
                for parent_target in URL_TAG_NAME_RE.findall(header.attrs["parent_url"]):
                    norm_label = VAR_PLACEHOLDER_RE.sub("{{ x }}", header.attrs["parent_label"].strip())
                    labels_by_parent.setdefault(parent_target.split(":")[-1], {}).setdefault(norm_label, []).append(
                        name
                    )

    for parent_url, label_map in labels_by_parent.items():
        if len(label_map) > 1:
            detail = ", ".join(f"{lbl!r} ({', '.join(files)})" for lbl, files in sorted(label_map.items()))
            problems[f"parent_label:{parent_url}"] = f"inconsistent parent_label across child pages: {detail}"

    fail_with("Page header hierarchy or parent_label inconsistency", problems, STANDALONE_HEADER_EXCEPTIONS)


def test_no_inline_styles() -> None:
    """A page's look comes from classes: a style attribute or <style> block is styling the vocabulary can't see."""
    lines: dict[str, list[str]] = {}
    for path in page_templates():
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
    for path in portal_templates():
        for tag in tags(path):
            name = tag.name.removeprefix("c-")
            if not tag.name.startswith("c-") or name not in known or "variant" not in tag.attrs:
                continue
            variant = tag.attrs["variant"]
            key = f'{template_name(path)}:<c-{name} variant="{variant}">'
            if "{{" in variant:
                if f"|{VARIANT_FILTERS.get(name)}" not in variant.replace(" ", ""):
                    found[key] = f"line {tag.line}: map the status with |{VARIANT_FILTERS.get(name, '...')}"
                elif name == "badge" and (problem := badge_kind_problem(variant)):
                    found[key] = f"line {tag.line}: {problem}"
            elif variant not in known[name]:
                found[key] = f"line {tag.line}: known: {', '.join(sorted(known[name]))}"
    fail_with("Unknown component variant", found, VARIANT_EXCEPTIONS)


def badge_kind_problem(variant: str) -> str:
    """Why a |badge_variant use is wrong: a kind core.status_variants doesn't have, or a field the kind isn't for."""
    m = VARIANT_FILTER_RE.match(variant.strip())
    if not m:
        return 'write it as {{ object.field|badge_variant:"kind" }}'
    kind, field = m.group("kind"), m.group("expr").split(".")[-1]
    if kind not in status_variants.BADGE_STATUSES:
        return f"no status kind {kind!r}; kinds: {', '.join(sorted(status_variants.BADGE_STATUSES))}"
    label, field_name, _ = status_variants.BADGE_STATUSES[kind]
    if field != field_name:
        return f"kind {kind!r} maps {label}.{field_name}, not {field}"
    return ""


def test_table_widths_are_lengths() -> None:
    """c-table's widths become inline styles, so each one must be a plain length, not CSS of the page's own."""
    found: dict[str, str] = {}
    for path in page_templates():
        for tag in tags(path):
            if tag.name != "c-table" or "widths" not in tag.attrs:
                continue
            tokens = re.sub(r"{%.*?%}", " ", tag.attrs["widths"]).split()
            bad = [t for t in tokens if not LENGTH_RE.fullmatch(t)]
            if bad:
                found[f"{template_name(path)}:widths"] = f"line {tag.line}: not a length: {' '.join(bad)}"
    fail_with("c-table widths that are not lengths (px, %, rem, ch or auto)", found, {})


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


def component_reads(path: Path, name: str) -> bool:
    """Whether the component's template reads `name` in a {{ }} or {% %} (comments aside)."""
    content = re.sub(r"{#.*?#}", "", text(path), flags=re.DOTALL)
    word = rf"(?<![\w.]){re.escape(name)}\b"
    return bool(re.search(rf"{{{{[^}}]*{word}|{{%[^%]*{word}", content))


def test_props_a_component_reads_are_declared() -> None:
    """A prop missing from <c-vars> also lands in {{ attrs }}, so it leaks out as an HTML attribute."""
    declared = {
        path.stem: {name for tag in tags(path) if tag.name == "c-vars" for name in tag.attrs}
        for path in shared_component_paths()
    }
    paths = {path.stem: path for path in shared_component_paths()}
    found: dict[str, str] = {}
    for page in portal_templates():
        for tag in tags(page):
            component = tag.name.removeprefix("c-")
            if not tag.name.startswith("c-") or component not in paths:
                continue
            for attr in tag.attrs:
                name = attr.lstrip(":")
                # attrs is cotton's own: the attributes the component doesn't declare
                if name != "attrs" and name not in declared[component] and component_reads(paths[component], name):
                    found[f"cotton/{component}.html:{name}"] = f"passed by {template_name(page)}; add it to <c-vars>"
    fail_with("Props a component reads but doesn't declare", found, {})


def test_every_declared_prop_has_a_default() -> None:
    """A <c-vars> name with no default that the tag doesn't pass reads the page's variable of the same name."""
    found = {
        f"cotton/{path.name}:{name}": 'give it a default (name="")'
        for path in shared_component_paths()
        for tag in tags(path)
        if tag.name == "c-vars"
        for name in tag.attrs
        if not re.search(rf'(?<![\w-]){re.escape(name)}="', text(path).split("<c-vars", 1)[1].split("/>", 1)[0])
    }
    fail_with("Component props without a default", found, {})
