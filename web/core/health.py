"""Liveness check: `python -m core.health worker|chat` exits 0 if every heartbeat that process writes is fresh, else
prints the problems and exits 1.

Intended as a Kubernetes exec liveness probe for the worker and chat pods (they have no HTTP listener).
Imports nothing from Django, so it can't hang on the database.
"""

import sys
import time
from collections.abc import Mapping

from core.heartbeat import budgets, heartbeat_dir

PROCESSES = ("worker", "chat")


def problems(allowed: Mapping[str, int]) -> list[str]:
    """One problem per stale or missing heartbeat among allowed (name to budget in seconds). Empty means healthy."""
    found = []
    now = time.time()
    for name, budget in allowed.items():
        path = heartbeat_dir() / name
        try:
            age = now - float(path.read_text())
        except OSError, ValueError:
            found.append(f"{name}: heartbeat missing ({path})")
            continue
        if age > budget:
            found.append(f"{name}: heartbeat stale ({age:.0f}s old, budget {budget}s)")
    return found


def check(process: str) -> list[str]:
    """Problems with process's heartbeats. Empty means healthy."""
    return problems(budgets(process))


def report(found: list[str]) -> int:
    """Print found (or "ok") and return the probe's exit code."""
    for problem in found:
        print(problem)
    if not found:
        print("ok")
    return 1 if found else 0


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in PROCESSES:
        print(f"usage: python -m core.health {'|'.join(PROCESSES)}")
        return 2
    return report(check(argv[0]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
