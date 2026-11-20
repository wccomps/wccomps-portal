"""The preflight report for the ops channel: which teams' services failed Quotient's latest preflight round, the checks
it runs before the competition starts and keeps apart from scoring."""

from datetime import datetime, timedelta

from quotient.client import EngineStatus

# A preflight round older than this is treated as none having run
STALE = timedelta(minutes=10)


def _team_order(name: str) -> tuple[int, str]:
    """team2 before team10."""
    return len(name), name


def preflight_report(status: EngineStatus | None, team_names: dict[int, str] | None, now: datetime) -> str:
    """The report: each team with a failing check and the services that failed, one team per line, or why there is no
    preflight round to report. A team Quotient's team list doesn't name (or a list it couldn't give) shows its ID."""
    if status is None:
        return "**Preflight:** couldn't read the engine status from Quotient."
    if status.preflight is None:
        return (
            "**Preflight:** can't read a preflight round: Quotient's engine status has no preflight field, so its "
            "build predates preflight rounds."
        )
    if not status.preflight:
        return (
            "**Preflight:** Quotient runs no preflight rounds now: it has scored a round or marked the competition "
            "started."
        )
    if not status.round_id or not status.round_start or now - status.round_start > STALE:
        minutes = int(STALE.total_seconds() // 60)
        why = "Quotient's engine is paused." if not status.running else "Is Quotient's engine stuck?"
        return f"**Preflight:** no preflight round in the last {minutes} minutes. {why}"

    header = f"**Preflight round {status.round_id}** (<t:{int(status.round_start.timestamp())}:t>)"
    if not status.checks:
        return f"{header}: it ran no checks. Does Quotient have its teams and services set up?"

    names = team_names or {}
    failed: dict[str, list[str]] = {}
    for check in status.checks:
        if not check.passed:
            failed.setdefault(names.get(check.team_id, f"team ID {check.team_id}"), []).append(check.service_name)
    teams = len({check.team_id for check in status.checks})
    if not failed:
        return f"{header}: all {teams} teams green."

    lines = [f"{header}: {teams - len(failed)}/{teams} teams green. Failing:"]
    lines += [f"- **{team}**: {', '.join(sorted(failed[team]))}" for team in sorted(failed, key=_team_order)]
    return "\n".join(lines)
