"""UI text uses words: one rule (ui_text_problems) over one list of where UI text appears (ui_text_sites).

The rule: no emoji, symbol marks or math signs standing for a status or a control, no em or en dashes, and no hyphen
standing in for a dash (" - ", "--") or for a missing value (a lone "-": an empty value is left blank). A page title
keeps its "Page - Section" separator.

Where UI text appears: a template's text (titles and textareas included, a template tag's quoted strings such as a
|default:"-" each read as a string of their own), its text attributes and component props (TEXT_ATTRIBUTES), the
strings in its inline scripts, and every string literal in the web app's Python but docstrings (messages, form
errors, strings returned for display and admin columns among them), with the bot's role sync, whose change list the
Sync Roles page shows. A string there that isn't UI text, or a symbol that is the control itself, is in
UI_TEXT_EXCEPTIONS with why. The rest of the bot writes Discord messages and is outside it.
"""

import ast
import re
import unicodedata
from collections.abc import Iterator
from pathlib import Path

from core.tests.guards import (
    REPO_DIR,
    fail_with,
    scripts,
    tags,
    template_name,
    text,
    text_nodes,
    tracked_files,
    tracked_templates,
)

DASHES = {"—": "em dash", "–": "en dash"}
LONE_HYPHEN_RE = re.compile(r"\s*-\s*")  # the whole string
HYPHEN_DASH_RE = re.compile(r"\s-\s")
DOUBLE_HYPHEN_RE = re.compile(r"(^|\s)--(\s|$)")

# Attributes and component props whose value a person reads
TEXT_ATTRIBUTES = {
    "alt",
    "aria-label",
    "aria_label",
    "data-confirm",
    "description",
    "heading",
    "help_text",
    "label",
    "parent_label",
    "placeholder",
    "subtitle",
    "title",
    "tooltip",
}

TITLE_RE = re.compile(r"{%\s*block\s+\w*title\s*%}.*?{%\s*endblock|<title>.*?</title>", re.DOTALL)
TEMPLATE_TAG_RE = re.compile(r"{%.*?%}|{{.*?}}", re.DOTALL)
QUOTED_RE = re.compile(r""""([^"]*)"|'([^']*)'""")
JS_STRING_RE = re.compile(r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`""")
CHROME = "labelled for screen readers"

# "file:line:problem" (fnmatch) -> why that string reads right, or isn't UI text
UI_TEXT_EXCEPTIONS = {
    "admin/base_site.html:*:☰": "the menu button's icon, " + CHROME,
    "admin/base_site.html:*:✕": "the menu's close icon, " + CHROME,
    "admin/base_site.html:*:☀": "the dark mode toggle's icon, " + CHROME,
    "admin/base_site.html:*:\U0001f319": "the dark mode toggle's icon, " + CHROME,
    "admin/base_site.html:*:\U0001f507": "the notification sound toggle's icon, " + CHROME,
    "admin/base_site.html:*:\U0001f514": "the notification sound toggle's icon, " + CHROME,
    "cotton/table_header.html:*:▲": "the sort direction arrow on the sorted column",
    "cotton/table_header.html:*:▼": "the sort direction arrow on the sorted column",
    "cotton/fieldset.html:*:▶": "the collapsible fieldset's expand arrow",
    "registration/register.html:*:©": "the copyright line",
    "scoring/_scorecard_body.html:*:×": "multiplication in the score formula",
    "scoring/scorecard_print.html:*:×": "multiplication in the score formula",
    # Hyphens in code, not text
    "cotton/table_header.html:*:hyphen for no value": "the descending sort prefix the sort links compare and add",
    "web/core/utils.py:*:hyphen for no value": "the descending sort prefix",
    "web/core/authentik_utils.py:*:hyphen for no value": "the separator in generated passwords and in team ranges",
    "web/core/csv_export.py:*:hyphen for no value": "a cell starting with it is a spreadsheet formula, so it's escaped",
    "web/core/middleware.py:*:hyphen for no value": "an empty field in the request log line",
    "web/gunicorn.conf.py:*:hyphen for no value": "gunicorn's name for standard output",
}


def is_symbol(character: str) -> bool:
    return ord(character) > 127 and unicodedata.category(character) in ("So", "Sk", "Sm")


def ui_text_problems(string: str) -> list[str]:
    """What in a string a person reads breaks the rule: each symbol mark or em/en dash (the character itself), and
    each misused hyphen."""
    problems = [c for c in dict.fromkeys(string) if c in DASHES or is_symbol(c)]
    if LONE_HYPHEN_RE.fullmatch(string):
        return [*problems, "hyphen for no value"]
    for problem, pattern in (("hyphen as a dash", HYPHEN_DASH_RE), ("double hyphen", DOUBLE_HYPHEN_RE)):
        if pattern.search(string):
            problems.append(problem)
    return problems


def template_strings(string: str) -> list[str]:
    """Template text as read: the text with each variable standing for a word (a tag for nothing), and each tag's
    quoted strings."""
    return [
        TEMPLATE_TAG_RE.sub(lambda m: "x" if m.group().startswith("{{") else "", string),
        *(a or b for tag in TEMPLATE_TAG_RE.findall(string) for a, b in QUOTED_RE.findall(tag)),
    ]


def title_lines(path: Path) -> set[int]:
    content = text(path)
    return {
        line
        for m in TITLE_RE.finditer(content)
        for line in range(content.count("\n", 0, m.start()) + 1, content.count("\n", 0, m.end()) + 2)
    }


def template_sites(path: Path) -> Iterator[tuple[int, str, bool]]:
    """Each string the template shows, with its line and whether it is page title text."""
    titles = title_lines(path)
    for start, words in text_nodes(path):
        for offset, line in enumerate(words.split("\n")):
            for string in template_strings(line):
                yield start + offset, string, start + offset in titles
    for tag in tags(path):
        for name, value in tag.attrs.items():
            if name in TEXT_ATTRIBUTES:
                for string in template_strings(value):
                    yield tag.line, string, False
    for start, script in scripts(path):
        for m in JS_STRING_RE.finditer(script):
            string = next(g for g in m.groups() if g is not None)
            yield start + script.count("\n", 0, m.start()), string, False


# Python modules held to the rule: the web app (not its tests or migrations) and the bot's role sync
PYTHON_UI_FILES_RE = re.compile(r"web/(?!.*/tests/|.*/migrations/|browser_tests/).*\.py|bot/role_sync\.py")


def is_log_call(node: ast.AST) -> bool:
    """logger.info(...), logging.warning(...) and the like: a log line, not UI text."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("logger", "logging", "log")
    )


def string_constants(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a module but its docstrings and log lines; an f-string reads as one string, each
    placeholder standing for a word."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if is_log_call(node):
            return
        if isinstance(node, ast.JoinedStr):
            parts = [v.value if isinstance(v, ast.Constant) else "x" for v in node.values]
            found.append((node.lineno, "".join(str(part) for part in parts)))
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    visit(value.value)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            found.append((node.lineno, node.value))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def ui_text_sites() -> Iterator[tuple[str, int, str, bool]]:
    """Every place UI text appears: (file, line, string, whether it is page title text)."""
    for path in tracked_templates():
        for line, string, is_title in template_sites(path):
            yield template_name(path), line, string, is_title
    for name in tracked_files():
        if PYTHON_UI_FILES_RE.fullmatch(name):
            for line, string in string_constants(REPO_DIR / name):
                yield name, line, string, False


def test_ui_text_follows_the_rule() -> None:
    found = {
        f"{where}:{line}:{problem}": repr(string.strip()[:70])
        for where, line, string, is_title in ui_text_sites()
        for problem in ui_text_problems(string)
        if not (is_title and problem == "hyphen as a dash")
    }
    fail_with("UI text breaking the rule (write the word; leave a missing value blank)", found, UI_TEXT_EXCEPTIONS)


def test_the_rule_finds_each_kind_of_problem() -> None:
    assert ui_text_problems("Done ✓") == ["✓"]
    assert ui_text_problems("a — b") == ["—"]
    assert ui_text_problems(" - ") == ["hyphen for no value"]
    assert ui_text_problems("Approved - Team 4") == ["hyphen as a dash"]
    assert ui_text_problems("wait -- what") == ["double hyphen"]
    assert ui_text_problems("1,3,5-10, e-mail, -created_at, --flag") == []


def test_template_sites_read_text_attributes_tags_and_scripts(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text(
        '{% block title %}Ops - Section{% endblock %}\n<c-module title="Edit - Team 4">{{ x|default:"-" }}</c-module>\n'
        "<script>alert('Saved ✓');</script>\n<p class=\"a - b\">fine</p>",
        encoding="utf-8",
    )
    sites = set(template_sites(page))
    assert (1, "Ops - Section", True) in sites
    assert (2, "Edit - Team 4", False) in sites
    assert (2, "-", False) in sites
    assert (3, "Saved ✓", False) in sites
    assert not any("a - b" in string for _, string, _ in sites)
