"""Shared pieces of the guard tests: the tracked files, which templates are standalone documents, and the
exceptions report.

A guard test lists every instance of a class of problem. An instance is fixed, or listed in that test's
exceptions with a one-line reason; an exception that no longer matches anything fails too, so the list can't
go stale.
"""

import fnmatch
import re
import subprocess
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[3]
WEB_DIR = REPO_DIR / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
# Template and HTML comments, which say <html> without being one
COMMENTS_RE = re.compile(r"{#.*?#}|{%\s*comment\s*%}.*?{%\s*endcomment\s*%}|<!--.*?-->", re.DOTALL)


@cache
def tracked_files() -> tuple[str, ...]:
    """Tracked files (repo-relative paths) that exist in the working tree.

    A tracked file deleted but not yet staged is left out, as it will be once committed.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return tuple(p for p in tracked if p and (REPO_DIR / p).is_file())


@cache
def tracked_templates() -> tuple[Path, ...]:
    return tuple(
        sorted(REPO_DIR / p for p in tracked_files() if p.startswith("web/templates/") and p.endswith(".html"))
    )


def template_name(path: Path) -> str:
    """The path Django loads a template by (admin/base_site.html)."""
    return path.relative_to(TEMPLATES_DIR).as_posix()


@cache
def is_standalone_document(path: Path) -> bool:
    """A template with an <html> of its own (emails, the print scorecard, the 500 page, the public registration
    form): it carries its own styles and never loads the portal's base template or app.css."""
    content = COMMENTS_RE.sub("", path.read_text(encoding="utf-8"))
    return bool(re.search(r"<html\b", content, re.IGNORECASE))


def fail_with(
    title: str,
    found: Mapping[str, str],
    exceptions: Mapping[str, str],
    also: Mapping[str, Iterable[str]] | None = None,
) -> None:
    """Fail listing each found key (with its detail) no exception covers, and each exception that covers nothing.

    Exception keys are fnmatch patterns over the found keys, or over a key's `also` names (e.g. an item's
    decorators), so one entry can cover a framework convention.
    """
    matched: set[str] = set()
    problems: list[str] = []
    for key, detail in found.items():
        names = (key, *(also or {}).get(key, ()))
        hits = {pattern for pattern in exceptions if any(fnmatch.fnmatchcase(name, pattern) for name in names)}
        matched |= hits
        if not hits:
            problems.append(f"{key} ({detail})" if detail else key)
    problems.sort()
    problems += [
        f"{pattern}: in the exceptions but matches nothing; drop it there"
        for pattern in exceptions
        if pattern not in matched
    ]
    if problems:
        pytest.fail(f"{title} ({len(problems)}):\n" + "\n".join(f"  {p}" for p in problems), pytrace=False)


# Template tags, read with their attributes (template tags inside a value kept as written)
COMMENT_RE = re.compile(r"{#.*?#}|{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.DOTALL)
TEMPLATE_TAG_RE = re.compile(r"{%.*?%}|{{.*?}}", re.DOTALL)
PLACEHOLDER_RE = re.compile("\x01(\\d+)\x02")
TAG_RE = re.compile(
    r"""<(?P<name>[A-Za-z][\w:.-]*)(?=[\s/>])(?P<attrs>(?:\s*[^\s=>/"']+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>"']+))?)*)\s*/?>"""
)
ATTR_RE = re.compile(r"""([^\s=>/"']+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>"']+))?""")


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
