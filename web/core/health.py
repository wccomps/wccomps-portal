"""Liveness check: `python -m core.health worker|chat` exits 0 if every heartbeat that process writes is fresh, else
prints the problems and exits 1.

Intended as a Kubernetes exec liveness probe for the worker and chat pods (they have no HTTP listener).
Imports nothing from Django, so it can't hang on the database.
"""

import sys
import time

from core.heartbeat import budgets, heartbeat_dir


def check(process: str) -> list[str]:
    """One problem per stale or missing heartbeat of process ("worker" or "chat"). Empty means healthy; ValueError
    for an unknown process."""
    found = []
    now = time.time()
    for name, budget in budgets(process).items():
        path = heartbeat_dir() / name
        try:
            age = now - float(path.read_text())
        except OSError, ValueError:
            found.append(f"{name}: heartbeat missing ({path})")
            continue
        if age > budget:
            found.append(f"{name}: heartbeat stale ({age:.0f}s old, budget {budget}s)")
    return found


def main(argv: list[str]) -> int:
    usage = "usage: python -m core.health worker|chat"
    if len(argv) != 1:
        print(usage)
        return 2
    try:
        found = check(argv[0])
    except ValueError as e:
        print(f"{usage}: {e}")
        return 2
    for problem in found:
        print(problem)
    if not found:
        print("ok")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
