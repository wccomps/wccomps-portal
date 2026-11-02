"""Every AuthentikManager the portal or bot opens is closed: each is made as a `with` statement's context."""

import ast

from core.tests.guards import REPO_DIR, fail_with, tracked_files

# "path:line" -> why that AuthentikManager is closed some other way
EXCEPTIONS: dict[str, str] = {}


def _unclosed(tree: ast.AST) -> list[int]:
    managed = {id(item.context_expr) for node in ast.walk(tree) if isinstance(node, ast.With) for item in node.items}
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AuthentikManager"
        and id(node) not in managed
    ]


def test_every_authentik_manager_is_opened_with_with():
    found = {}
    for name in tracked_files():
        if not name.endswith(".py") or "/tests/" in name or "browser_tests/" in name or "/migrations/" in name:
            continue
        source = (REPO_DIR / name).read_text()
        if "AuthentikManager(" not in source:
            continue
        for line in _unclosed(ast.parse(source)):
            found[f"{name}:{line}"] = source.splitlines()[line - 1].strip()
    fail_with("An AuthentikManager never closed (open it with `with AuthentikManager() as ...`)", found, EXCEPTIONS)
