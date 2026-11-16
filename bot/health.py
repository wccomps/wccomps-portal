"""Bot liveness check while the bot still hosts the worker's queue and timer: `python -m bot.health` exits 0 if every
heartbeat in BOT_BUDGETS is fresh, else prints the problems and exits 1.

Production's bot liveness probe runs this until it moves to `python -m core.health chat`, when the worker process
takes the queue and timer over. Imports nothing from Django, so it can't hang on the database.
"""

import sys

from core.health import problems, report
from core.heartbeat import DISCORD_BUDGETS, WORKER_BUDGETS

# The loops main.py runs: the Discord ones, plus the worker queue and competition timer it hosts for now
BOT_BUDGETS: dict[str, int] = {
    **DISCORD_BUDGETS,
    "queue-worker": WORKER_BUDGETS["queue-worker"],
    "timer": WORKER_BUDGETS["timer"],
}


def check() -> list[str]:
    """Problems found, one per stale or missing loop heartbeat. Empty means healthy."""
    return problems(BOT_BUDGETS)


if __name__ == "__main__":
    sys.exit(report(check()))
