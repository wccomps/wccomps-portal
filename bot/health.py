"""Bot liveness check until production's probe moves to `python -m core.health chat`: `python -m bot.health` exits 0
if every heartbeat in BOT_BUDGETS is fresh, else prints the problems and exits 1.

Imports nothing from Django, so it can't hang on the database.
"""

import sys

from core.health import problems, report
from core.heartbeat import DISCORD_BUDGETS

# The loops main.py runs: the Discord queue and the dashboard. The worker process runs the rest.
BOT_BUDGETS: dict[str, int] = dict(DISCORD_BUDGETS)


def check() -> list[str]:
    """Problems found, one per stale or missing loop heartbeat. Empty means healthy."""
    return problems(BOT_BUDGETS)


if __name__ == "__main__":
    sys.exit(report(check()))
