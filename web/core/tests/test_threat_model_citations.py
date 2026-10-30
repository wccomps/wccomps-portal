"""THREAT_MODEL.md cites the code behind each mitigation as `path` `name`; a cited file or name that no longer
exists fails here, so a rename or move updates the threat model in the same change."""

import ast
import re
from pathlib import PurePosixPath

from core.tests.guards import REPO_DIR, tracked_files

MODEL = (REPO_DIR / "THREAT_MODEL.md").read_text()
FILE = re.compile(r"[\w.-]+(?:/[\w.-]+)*\.(?:py|yml|yaml|toml|lock|md|html|js|css)")
# A file path span, then its names: `web/core/oauth.py` `oauth_login`, `oauth_callback`
CITATION = re.compile(r"`([^`\s]+)`((?:,? `[A-Za-z_][\w.]*`)+)")


def spans() -> list[str]:
    return re.findall(r"`([^`\n]+)`", MODEL)


def defined_names(path: str) -> set[str]:
    """Functions and classes (dotted through their class), and names assigned outside any function."""
    names: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(prefix + node.name)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(prefix + t.id for t in targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.If | ast.Try | ast.With):
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []), prefix)

    visit(ast.parse((REPO_DIR / path).read_text()).body, "")
    return names


def test_every_cited_file_exists() -> None:
    tracked = set(tracked_files())
    basenames = {PurePosixPath(p).name for p in tracked}
    cited = [s for s in spans() if FILE.fullmatch(s)]
    missing = [s for s in cited if s not in tracked and ("/" in s or s not in basenames)]

    assert len(cited) > 30, "the citation pattern no longer finds the threat model's file citations"
    assert not missing, f"THREAT_MODEL.md cites files that don't exist: {missing}"


def test_every_cited_name_is_defined_in_its_file() -> None:
    checked = 0
    missing = []
    for match in CITATION.finditer(MODEL):
        path = match.group(1)
        if not path.endswith(".py"):
            continue
        defined = defined_names(path)
        for name in re.findall(r"`([^`]+)`", match.group(2)):
            if FILE.fullmatch(name):
                break
            checked += 1
            if name not in defined:
                missing.append(f"{path} {name}")

    assert checked > 30, "the citation pattern no longer finds the threat model's named citations"
    assert not missing, f"THREAT_MODEL.md cites names its files don't define: {missing}"
