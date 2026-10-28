"""Shared pieces of the guard tests: the tracked files, which templates are standalone documents, and the
exceptions report.

A guard test lists every instance of a class of problem. An instance is fixed, or listed in that test's
exceptions with a one-line reason; an exception that no longer matches anything fails too, so the list can't
go stale.
"""

import fnmatch
import re
import subprocess
from collections.abc import Iterable, Iterator, Mapping
from functools import cache
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[3]
WEB_DIR = REPO_DIR / "web"
TEMPLATES_DIR = WEB_DIR / "templates"


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


@cache
def component_templates() -> tuple[Path, ...]:
    """The shared components, templates/cotton/."""
    return tuple(p for p in tracked_templates() if p.is_relative_to(TEMPLATES_DIR / "cotton"))


@cache
def portal_templates() -> tuple[Path, ...]:
    """Every template the portal frames: all but the standalone documents (components included)."""
    return tuple(p for p in tracked_templates() if not is_standalone_document(p))


@cache
def page_templates() -> tuple[Path, ...]:
    """Pages and the includes beside them: every template but the components and the standalone documents."""
    components = set(component_templates())
    return tuple(p for p in tracked_templates() if p not in components and not is_standalone_document(p))


def template_name(path: Path) -> str:
    """The path Django loads a template by (admin/base_site.html)."""
    return path.relative_to(TEMPLATES_DIR).as_posix()


@cache
def is_standalone_document(path: Path) -> bool:
    """A template with its own <html>, or a part only such a page renders (registration/public/): neither loads the
    portal's base template or app.css."""
    return template_name(path).startswith("registration/public/") or any(tag.name == "html" for tag in elements(path))


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


FIELD_GROUP = "field-group"
FIELD_GROUP_RE = re.compile(r"{{\s*\w*form\.(\w+)\.as_field_group\s*}}")

# HTML and component tags, with any {{ }} or {% %} in an attribute kept as written
COMMENT_RE = re.compile(r"{#.*?#}|{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", re.DOTALL)
TEMPLATE_TAG_RE = re.compile(r"{%.*?%}|{{.*?}}", re.DOTALL)
PLACEHOLDER_RE = re.compile("\x01(\\d+)\x02")
# HTMLParser decodes entities in attribute values; this stands in for & so values come back as written
AMPERSAND = "\x03"


class Tag:
    """A start or end tag of an element or component, with template tags in its attribute values kept as written."""

    def __init__(self, name: str, attrs: dict[str, str], line: int, closing: bool = False) -> None:
        self.name = name
        self.attrs = attrs
        self.line = line
        self.closing = closing


@cache
def text(path: Path) -> str:
    """The file without template comments, keeping their line breaks so line numbers still match."""
    return COMMENT_RE.sub(lambda m: "\n" * m.group().count("\n"), path.read_text(encoding="utf-8"))


class TagReader(HTMLParser):
    def __init__(self, kept: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.kept = kept
        self.found: list[Tag] = []
        self.texts: list[tuple[int, str]] = []
        self.scripts: list[tuple[int, str]] = []
        self.in_script = False

    def restore(self, value: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: self.kept[int(m.group(1))], value.replace(AMPERSAND, "&"))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {}
        for name, value in attrs:
            # An attribute inside {% if %}...{% endif %} reads as the attribute itself
            name = PLACEHOLDER_RE.sub("", name)
            if name:
                values[name] = self.restore(value or "")
        self.found.append(Tag(tag, values, self.getpos()[0]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.found.append(Tag(tag, {}, self.getpos()[0], closing=True))

    def handle_endtag(self, tag: str) -> None:
        self.found.append(Tag(tag, {}, self.getpos()[0], closing=True))
        self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.cdata_elem == "script":
            if not self.in_script:
                self.scripts.append((self.getpos()[0], ""))
                self.in_script = True
            line, script = self.scripts[-1]
            self.scripts[-1] = (line, script + self.restore(data))
        # cdata_elem: inside a raw text element (script, style, xmp, iframe, noembed, noframes, title, textarea);
        # of those, a title's and a textarea's text is shown
        if self.cdata_elem in (None, "title", "textarea") and data.strip():
            self.texts.append((self.getpos()[0], self.restore(data)))
        if self.cdata_elem is None:
            # A form row a field renders ({{ form.x.as_field_group }}) reads as a void element of its own
            for placeholder in PLACEHOLDER_RE.finditer(data):
                if group := FIELD_GROUP_RE.fullmatch(self.kept[int(placeholder.group(1))]):
                    line = self.getpos()[0] + data[: placeholder.start()].count("\n")
                    self.found.append(Tag(FIELD_GROUP, {"field": group.group(1)}, line))


@cache
def read(path: Path) -> TagReader:
    """The template read by HTMLParser. Template tags are set aside first, so a ">" or quote inside one
    ({% if n > 0 %}) neither ends a tag nor its attribute."""
    kept: list[str] = []

    def hide(match: re.Match[str]) -> str:
        kept.append(match.group())
        return f"\x01{len(kept) - 1}\x02"

    reader = TagReader(kept)
    reader.feed(TEMPLATE_TAG_RE.sub(hide, text(path)).replace("&", AMPERSAND))
    reader.close()
    return reader


def elements(path: Path) -> tuple[Tag, ...]:
    """Every start and end tag in the template, in order. A self-closing or void tag has no end tag."""
    return tuple(read(path).found)


def text_nodes(path: Path) -> tuple[tuple[int, str], ...]:
    """The text a page shows, with its line: between tags, titles and textareas included, the other raw text
    elements (scripts, styles, xmp, iframe, noembed, noframes) left out, template tags kept as written."""
    return tuple(read(path).texts)


# A comment, or a string (left as written, so a "//" in a URL is no comment)
JS_COMMENT_OR_STRING_RE = re.compile(
    r"""//[^\n]*|/\*.*?\*/|'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`""", re.DOTALL
)


def without_js_comments(source: str) -> str:
    """The JavaScript with its comments blanked (line breaks kept), strings left as written."""
    return JS_COMMENT_OR_STRING_RE.sub(
        lambda m: m.group() if m.group()[0] in "'\"`" else "\n" * m.group().count("\n"), source
    )


def scripts(path: Path) -> tuple[tuple[int, str], ...]:
    """Each inline <script>'s JavaScript with the line it starts on, comments left out, template tags kept as
    written."""
    return tuple((line, without_js_comments(script)) for line, script in read(path).scripts)


@cache
def static_scripts() -> tuple[tuple[str, str], ...]:
    """Each tracked static JavaScript file (repo-relative path) and its JavaScript, comments left out."""
    return tuple(
        (name, without_js_comments((REPO_DIR / name).read_text(encoding="utf-8")))
        for name in tracked_files()
        if name.startswith("web/static/js/") and name.endswith(".js")
    )


EXTENDS_RE = re.compile(r"""{%\s*extends\s+["']([^"']+)["']\s*%}""")


@cache
def extends_chain(path: Path, root: Path = TEMPLATES_DIR) -> tuple[str, ...]:
    """The template's name (from root) and the name of each template it extends, nearest first. A parent outside
    root (Django's own admin templates) ends the chain."""
    chain = [path.relative_to(root).as_posix()]
    while (path := root / chain[-1]).is_file() and (match := EXTENDS_RE.search(text(path))):
        if match.group(1) in chain:
            break
        chain.append(match.group(1))
    return tuple(chain)


@cache
def tags(path: Path) -> tuple[Tag, ...]:
    """Every start tag in the template (see elements)."""
    return tuple(tag for tag in elements(path) if not tag.closing)


VOID = {
    *("input", "br", "img", "hr", "meta", "link", "source", "col", "wbr", "area", "base", "embed", "track"),
    FIELD_GROUP,
}


def with_open_elements(path: Path) -> Iterator[tuple[Tag, list[Tag]]]:
    """Each start tag with the elements open around it, outermost first."""
    open_tags: list[Tag] = []
    for tag in elements(path):
        if tag.closing:
            names = [t.name for t in open_tags]
            if tag.name in names:
                del open_tags[len(names) - 1 - names[::-1].index(tag.name) :]
            continue
        yield tag, list(open_tags)
        if tag.name not in VOID:
            open_tags.append(tag)
