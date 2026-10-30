"""THREAT_MODEL.md cites the code behind a mitigation as `path`, or `path` `name`, `name`; a cited file or name that no
longer exists fails here, so a rename or move updates the threat model in the same change."""

import ast
import re

from core.tests.guards import REPO_DIR, tracked_files

MODEL = (REPO_DIR / "THREAT_MODEL.md").read_text()
SPAN = re.compile(r"`([^`\n]+)`")
FILE = re.compile(r"(?:[\w.-]+/)*(?:Dockerfile|[\w.-]+\.(?:py|yml|yaml|toml|lock|md|html|js|css))")
NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
# A citation may wrap once; a blank line ends it, so it can't take the next paragraph's names
WRAP = r"(?:[ \t]+|[ \t]*\n[ \t]*)"


def read_citations() -> tuple[list[tuple[str, list[str]]], list[list[str]]]:
    """Each cited file with the names after it, and each run of two or more names with no file before it (a lone name
    is prose: `SECRET_KEY`, `next`)."""
    spans = list(SPAN.finditer(MODEL))

    def is_name(i: int) -> bool:
        text = spans[i].group(1)
        return bool(NAME.fullmatch(text)) and not FILE.fullmatch(text)

    def names_from(i: int, first_separator: str) -> list[str]:
        names: list[str] = []
        separator = first_separator
        while i < len(spans) and re.fullmatch(separator, MODEL[spans[i - 1].end() : spans[i].start()]) and is_name(i):
            names.append(spans[i].group(1))
            i += 1
            separator = "," + WRAP
        return names

    cited: list[tuple[str, list[str]]] = []
    orphans: list[list[str]] = []
    i = 0
    while i < len(spans):
        text = spans[i].group(1)
        if FILE.fullmatch(text):
            names = names_from(i + 1, WRAP)
            cited.append((text, names))
            i += 1 + len(names)
        elif is_name(i):
            names = [text, *names_from(i + 1, "," + WRAP)]
            if len(names) > 1:
                orphans.append(names)
            i += len(names)
        else:
            i += 1
    return cited, orphans


def defined_names(path: str) -> set[str]:
    """Functions and classes (dotted through their class), and names assigned outside any function."""
    names: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(prefix + node.name)
                continue
            if isinstance(node, ast.ClassDef):
                names.add(prefix + node.name)
                visit(node.body, f"{prefix}{node.name}.")
                continue
            if isinstance(node, ast.Assign | ast.AnnAssign):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    elements = target.elts if isinstance(target, ast.Tuple) else [target]
                    names.update(prefix + e.id for e in elements if isinstance(e, ast.Name))
            for block in ("body", "orelse", "finalbody"):
                visit(getattr(node, block, []), prefix)
            for branch in [*getattr(node, "handlers", []), *getattr(node, "cases", [])]:
                visit(branch.body, prefix)

    visit(ast.parse((REPO_DIR / path).read_text()).body, "")
    return names


def test_every_cited_file_exists() -> None:
    cited, _ = read_citations()
    tracked = set(tracked_files())
    missing = [path for path, _ in cited if path not in tracked]

    assert len(cited) > 30, "the citation pattern no longer finds the threat model's file citations"
    assert not missing, f"THREAT_MODEL.md cites files that aren't in the repo (give the full path): {missing}"


def test_every_cited_name_is_defined_in_its_file() -> None:
    cited, _ = read_citations()
    tracked = set(tracked_files())
    checked = 0
    problems = []
    for path, names in cited:
        if names and not path.endswith(".py"):
            problems.append(f"{path} names {names}, but only a Python file's names are checked")
        if not names or path not in tracked or not path.endswith(".py"):
            continue
        defined = defined_names(path)
        checked += len(names)
        problems += [f"{path} {name}" for name in names if name not in defined]

    assert checked > 30, "the citation pattern no longer finds the threat model's named citations"
    assert not problems, f"THREAT_MODEL.md cites names its files don't define: {problems}"


def test_cited_names_follow_their_file() -> None:
    _, orphans = read_citations()

    assert not orphans, f"THREAT_MODEL.md cites names with no file before them: {orphans}"
