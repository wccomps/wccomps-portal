"""The chat and worker processes never import each other's code: what both use lives in core, so each process starts
only its own loops, the chat code can't come to depend on the worker's, and the worker never loads a chat client. A
chat process without Discord never loads discord.py or the bot."""

import ast
import os
import subprocess
import sys

from core.tests.guards import REPO_DIR, tracked_files

CHAT_PACKAGES = ("bot", "chat", "discord")


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
    return found


def _imports_of(path: str, packages: tuple[str, ...]) -> list[str]:
    modules = _imported_modules(ast.parse((REPO_DIR / path).read_text(encoding="utf-8")))
    return sorted(m for m in modules if m.split(".")[0] in packages)


def _python_files(*packages: str) -> list[str]:
    return [p for p in tracked_files() if p.endswith(".py") and p.split("/")[0] in packages]


def test_the_chat_process_imports_nothing_from_the_worker() -> None:
    chat_files = _python_files("bot", "chat")
    assert any(p.startswith("bot/") for p in chat_files) and any(p.startswith("chat/") for p in chat_files)
    assert {path: found for path in chat_files if (found := _imports_of(path, ("worker",)))} == {}


def test_the_worker_imports_nothing_from_chat() -> None:
    worker_files = _python_files("worker")
    assert worker_files
    assert {path: found for path in worker_files if (found := _imports_of(path, CHAT_PACKAGES))} == {}


def test_a_zulip_only_chat_process_never_loads_discord() -> None:
    """Importing the chat process's entry point, in a fresh interpreter with only Zulip configured, loads neither
    discord.py nor the bot's code."""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "portal.settings",
        "PYTHONPATH": os.pathsep.join([str(REPO_DIR / "web"), str(REPO_DIR)]),
        "DISCORD_BOT_TOKEN": "",
        "ZULIP_SITE": "https://zulip.example.org",
        "ZULIP_BOT_EMAIL": "portal-bot@zulip.example.org",
        "ZULIP_API_KEY": "k",
    }
    code = "import sys, chat.__main__; print(sorted({m.split('.')[0] for m in sys.modules} & {'bot', 'discord'}))"
    # Fixed command running this interpreter, not untrusted input
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=REPO_DIR, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
