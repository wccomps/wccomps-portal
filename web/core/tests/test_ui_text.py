"""UI text uses words. In templates and Django admin columns: no emoji, symbol marks or math signs standing for a
status or a control, no em or en dashes, and no hyphen standing in for a dash (" - ", "--") or for a missing value;
an empty value is left blank, and page titles keep their "Page - WCComps Portal" separator. In every string literal
of the web app's Python (admin.py included) and the bot's role sync: no symbol marks, em or en dashes, or "--" (the
spaced hyphen isn't checked there yet).

A symbol that is the control itself (a menu toggle, a sort arrow) is listed in SYMBOL_EXCEPTIONS with why.
"""

import ast
import re
import unicodedata
from pathlib import Path

from core.tests.guards import REPO_DIR, fail_with, template_name, text, text_nodes, tracked_files, tracked_templates

DASHES = {"—": "em dash", "–": "en dash"}
# A lone hyphen standing for "no value": a |default:"-", a cell or span holding only "-", or an admin column's "-"
DASH_PLACEHOLDER_RE = re.compile(r"""default:\s*["']-["']|>\s*-\s*<|return\s+["']-["']""")
# In a template's text: a hyphen alone (a missing value), one between spaces or doubled (a dash)
TEXT_DASH_RE = re.compile(r"^\s*-\s*$|\s-\s|--")
TITLE_RE = re.compile(r"{%\s*block\s+\w*title\s*%}.*?{%\s*endblock|<title>.*?</title>", re.DOTALL)

# "file:character" -> why that symbol is the right UI there
SYMBOL_EXCEPTIONS = {
    "admin/base_site.html:☰": "the menu button's icon (labelled for screen readers)",
    "admin/base_site.html:✕": "the menu's close icon (labelled for screen readers)",
    "admin/base_site.html:☀": "the dark mode toggle's icon (labelled for screen readers)",
    "admin/base_site.html:\U0001f319": "the dark mode toggle's icon (labelled for screen readers)",
    "admin/base_site.html:\U0001f507": "the notification sound toggle's icon (labelled for screen readers)",
    "admin/base_site.html:\U0001f514": "the notification sound toggle's icon (labelled for screen readers)",
    "cotton/table_header.html:▲": "the sort direction arrow on the sorted column",
    "cotton/table_header.html:▼": "the sort direction arrow on the sorted column",
    "cotton/fieldset.html:▶": "the collapsible fieldset's expand arrow",
    "registration/register.html:©": "the copyright line",
    "scoring/_scorecard_body.html:×": "multiplication in the score formula",
    "scoring/scorecard_print.html:×": "multiplication in the score formula",
}


def ui_files() -> list[tuple[str, Path]]:
    admin_files = [REPO_DIR / p for p in tracked_files() if re.fullmatch(r"web/\w+/admin(/\w+)?\.py", p)]
    return [(template_name(p), p) for p in tracked_templates()] + [
        (p.relative_to(REPO_DIR).as_posix(), p) for p in admin_files
    ]


def lines_of(path: Path) -> list[str]:
    """A template's lines without its template comments, or a Python file's lines."""
    return (text(path) if path.suffix == ".html" else path.read_text(encoding="utf-8")).splitlines()


def is_symbol(character: str) -> bool:
    return ord(character) > 127 and unicodedata.category(character) in ("So", "Sk", "Sm")


def test_ui_text_has_no_symbol_marks() -> None:
    found = {}
    for name, path in ui_files():
        for number, line in enumerate(lines_of(path), 1):
            for character in line:
                if is_symbol(character):
                    found.setdefault(f"{name}:{character}", f"line {number}, {unicodedata.name(character, '?')}")
    fail_with("Symbols in UI text (write the word)", found, SYMBOL_EXCEPTIONS)


def test_ui_text_has_no_dashes() -> None:
    found = {}
    for name, path in ui_files():
        for number, line in enumerate(lines_of(path), 1):
            for dash, what in DASHES.items():
                if dash in line:
                    found[f"{name}:{number}"] = what
            if DASH_PLACEHOLDER_RE.search(line):
                found[f"{name}:{number}"] = "a dash standing for no value (leave it blank)"
        if path.suffix == ".html":
            content = text(path)
            titles = [
                range(content.count("\n", 0, m.start()) + 1, content.count("\n", 0, m.end()) + 2)
                for m in TITLE_RE.finditer(content)
            ]
            for start, words in text_nodes(path):
                for offset, words_line in enumerate(words.split("\n")):
                    number = start + offset
                    if TEXT_DASH_RE.search(words_line) and not any(number in lines for lines in titles):
                        found[f"{name}:{number}"] = f"a hyphen used as a dash or for no value: {words_line.strip()!r}"
    fail_with("Dashes in UI text", found, {})


# Python modules held to the rule: every module in the web app (not its tests or migrations), Discord messages
# built there included, and the bot's role sync, whose change list the Sync Roles page shows. The rest of the bot
# writes Discord messages and is outside it.
PYTHON_UI_FILES_RE = re.compile(r"web/(?!.*/tests/|.*/migrations/|browser_tests/).*\.py|bot/role_sync\.py")


def string_constants(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a module but its docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_ui_text_built_in_python_has_no_symbol_marks_or_dashes() -> None:
    """Every string literal in web/ (not tests, migrations or docstrings) and bot/role_sync.py: no symbol marks, em or
    en dashes, or "--". Discord messages built in web/ are held to it too."""
    found = {}
    for name in tracked_files():
        if not PYTHON_UI_FILES_RE.fullmatch(name):
            continue
        for number, value in string_constants(REPO_DIR / name):
            marks = sorted({c for c in value if c in DASHES or is_symbol(c)})
            if marks or re.search(r"(^|\s)--(\s|$)", value):
                found[f"{name}:{number}"] = f"{''.join(marks) or '--'} in {value.strip()[:60]!r}"
    fail_with("Symbols or dashes in UI text built in Python", found, {})
