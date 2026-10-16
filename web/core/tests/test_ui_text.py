"""UI text (templates and Django admin columns) uses words: no emoji or symbol marks standing for a status, and no
dashes, em dashes or en dashes, whether as an empty-cell placeholder or in prose. An empty value is left blank.

A symbol that is the control itself (a menu toggle, a sort arrow) is listed in SYMBOL_EXCEPTIONS with why.
"""

import re
import unicodedata
from pathlib import Path

from core.tests.guards import REPO_DIR, fail_with, template_name, tracked_files, tracked_templates

DASHES = {"—": "em dash", "–": "en dash"}
# A lone hyphen standing for "no value": a |default:"-", a cell or span holding only "-", or an admin column's "-"
DASH_PLACEHOLDER_RE = re.compile(r"""default:\s*["']-["']|>\s*-\s*<|return\s+["']-["']""")

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
}


def ui_files() -> list[tuple[str, Path]]:
    admin_files = [REPO_DIR / p for p in tracked_files() if re.fullmatch(r"web/\w+/admin(/\w+)?\.py", p)]
    return [(template_name(p), p) for p in tracked_templates()] + [
        (p.relative_to(REPO_DIR).as_posix(), p) for p in admin_files
    ]


def is_symbol(character: str) -> bool:
    return ord(character) > 127 and unicodedata.category(character) in ("So", "Sk")


def test_ui_text_has_no_symbol_marks() -> None:
    found = {}
    for name, path in ui_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for character in line:
                if is_symbol(character):
                    found.setdefault(f"{name}:{character}", f"line {number}, {unicodedata.name(character, '?')}")
    fail_with("Symbols in UI text (write the word)", found, SYMBOL_EXCEPTIONS)


def test_ui_text_has_no_dashes() -> None:
    found = {}
    for name, path in ui_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for dash, what in DASHES.items():
                if dash in line:
                    found[f"{name}:{number}"] = what
            if DASH_PLACEHOLDER_RE.search(line):
                found[f"{name}:{number}"] = "a dash standing for no value (leave it blank)"
    fail_with("Dashes in UI text", found, {})
