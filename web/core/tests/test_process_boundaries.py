"""The chat process never imports the worker's: what both use lives in core, so each process starts only its own loops
and the bot's code can't come to depend on the worker's."""

import ast

from core.tests.guards import REPO_DIR, tracked_files


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def _worker_imports(path: str) -> list[str]:
    modules = _imported_modules(ast.parse((REPO_DIR / path).read_text(encoding="utf-8")))
    return sorted(m for m in modules if m == "worker" or m.startswith("worker."))


def test_the_bot_imports_nothing_from_the_worker() -> None:
    bot_files = [p for p in tracked_files() if p.endswith(".py") and (p.startswith("bot/") or p == "main.py")]
    assert bot_files
    assert {path: found for path in bot_files if (found := _worker_imports(path))} == {}
