""".env.example lists every environment variable the portal reads, and nothing it doesn't.

Read: a literal name in `os.environ.get(...)`, `os.getenv(...)` or `os.environ[...]` in tracked production Python
(tests, conftest.py, migrations and browser tests excluded): web/portal/settings.py, and the readers that skip Django
settings (web/gunicorn.conf.py, web/core/heartbeat.py, web/core/discord_logging.py, bot/client.py). Also a variable
docker-compose.yml interpolates (`${NAME}`, `${NAME:-default}`), since .env.example is copied to compose's `.env`, which
compose reads for interpolation as well as passing it to every service.

Listed: a line `NAME=value`, or a commented `# NAME=value` for a setting left at its default (an empty assignment
would set it empty, which is not the default for most of them).
"""

import ast
import re
from functools import cache

from core.tests.guards import REPO_DIR, tracked_files

EXAMPLE = REPO_DIR / ".env.example"
COMPOSE = REPO_DIR / "docker-compose.yml"
NOT_PRODUCTION = re.compile(r"/migrations/|(^|/)tests/|^web/browser_tests/|(^|/)test_[^/]*\.py$|(^|/)conftest\.py$")
LISTED = re.compile(r"^(?:#\s*)?([A-Z][A-Z0-9_]*)=", re.MULTILINE)
INTERPOLATED = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)")

# Read but deliberately not listed, each with why
NOT_LISTED = {
    "HOSTNAME": "set by the container runtime; the error webhook names the host with it",
}


def _is_os_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_names(tree: ast.Module) -> set[str]:
    """The literal names a module reads from the environment."""
    names: set[str] = set()
    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Call) and node.args and isinstance(node.func, ast.Attribute):
            func = node.func
            if (func.attr == "get" and _is_os_environ(func.value)) or (
                func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os"
            ):
                key = node.args[0]
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _is_os_environ(node.value):
            key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
    return names


@cache
def read_names() -> dict[str, str]:
    """Each name the portal reads from the environment, with the first file that reads it."""
    found: dict[str, str] = {}
    for path in tracked_files():
        if not path.endswith(".py") or NOT_PRODUCTION.search(path):
            continue
        for name in sorted(_env_names(ast.parse((REPO_DIR / path).read_text(encoding="utf-8"), filename=path))):
            found.setdefault(name, path)
    for name in INTERPOLATED.findall(COMPOSE.read_text(encoding="utf-8")):
        found.setdefault(name, "docker-compose.yml")
    return found


def listed_names() -> list[str]:
    return LISTED.findall(EXAMPLE.read_text(encoding="utf-8"))


def test_the_readers_are_found() -> None:
    """The scan still sees settings.py and a reader outside it, so a change to how settings are read can't empty it."""
    found = read_names()
    assert found.get("DJANGO_SECRET_KEY") == "web/portal/settings.py"
    assert found.get("BOT_HEARTBEAT_DIR") == "web/core/heartbeat.py"
    assert found.get("PORTAL_TAG") == "docker-compose.yml"


def test_every_read_name_is_listed() -> None:
    listed = set(listed_names())
    missing = {name: path for name, path in read_names().items() if name not in listed and name not in NOT_LISTED}
    assert not missing, f".env.example doesn't list these environment variables (name: file reading it): {missing}"


def test_every_listed_name_is_read() -> None:
    unused = sorted(set(listed_names()) - set(read_names()))
    assert not unused, f".env.example lists names nothing reads: {unused}"


def test_no_name_is_listed_twice() -> None:
    names = listed_names()
    twice = sorted({name for name in names if names.count(name) > 1})
    assert not twice, f".env.example lists these more than once: {twice}"


def test_exceptions_are_read_and_unlisted() -> None:
    stale = sorted(name for name in NOT_LISTED if name not in read_names() or name in listed_names())
    assert not stale, f"NOT_LISTED entries no longer needed: {stale}"
