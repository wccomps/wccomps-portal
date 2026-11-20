"""The preflight report for the ops channel: which teams' services failed Quotient's latest preflight round, the checks
it runs before the competition starts and keeps apart from scoring."""

from datetime import datetime, timedelta

from quotient.client import EngineStatus, get_quotient_client

# A preflight round older than this is treated as none having run
STALE = timedelta(minutes=10)


def preflight_report(status: EngineStatus | None, now: datetime) -> str:
    """The ops report of Quotient's preflight round; a team is named by its Quotient ID, taken as its portal team
    number."""
    if status is None:
        return "**Preflight:** couldn't read the engine status from Quotient."
    if status.preflight is None:
        return (
            "**Preflight:** can't read a preflight round: Quotient's engine status has no preflight field, so its "
            "build predates the preflight field in GET /api/engine (upstream Quotient PR #208)."
        )
    if not status.preflight:
        return (
            "**Preflight:** Quotient runs no preflight rounds now: it has scored a round or marked the competition "
            "started."
        )
    if not status.round_id or not status.round_start or now - status.round_start > STALE:
        why = "Quotient's engine is paused." if not status.running else "Is Quotient's engine stuck?"
        return f"**Preflight:** no preflight round in the last {STALE // timedelta(minutes=1)} minutes. {why}"

    header = f"**Preflight round {status.round_id}** (<t:{int(status.round_start.timestamp())}:t>)"
    if not status.checks:
        return f"{header}: it ran no checks. Does Quotient have its teams and services set up?"

    failed: dict[int, list[str]] = {}
    for check in status.checks:
        if not check.passed:
            failed.setdefault(check.team_id, []).append(check.service_name)
    teams = len({check.team_id for check in status.checks})
    if not failed:
        return f"{header}: all {teams} teams green."

    lines = [f"{header}: {teams - len(failed)}/{teams} teams green. Failing:"]
    lines += [f"- **Team {team}**: {', '.join(sorted(failed[team]))}" for team in sorted(failed)]
    return "\n".join(lines)


def read_preflight_report(now: datetime) -> str:
    """The report from Quotient's engine status now; blocking (HTTP)."""
    return preflight_report(get_quotient_client().get_engine_status(), now)
