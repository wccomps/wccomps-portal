"""Watches Quotient for staff: a preflight report before the start, and an alert when a team goes dark."""

import logging
import re
from datetime import datetime, timedelta

from asgiref.sync import sync_to_async
from discord.ext import commands, tasks
from django.utils import timezone
from quotient.client import TRUNCATED, PreflightRound, TeamOutage, get_quotient_client

from bot.utils import log_to_ops_channel, recycle_db_connection, split_message
from core.log_safety import code_span
from core.models import CompetitionConfig

logger = logging.getLogger(__name__)

# Minutes before the scheduled start at which the preflight report is posted
PREFLIGHT_REPORT_MINUTES = (30, 10)
# A preflight round older than this is treated as none having run
PREFLIGHT_STALE = timedelta(minutes=10)
_FAILURE_RE = re.compile(r"FAIL(?:URE|ED):")
# Where Quotient starts a custom check's output, after the command line (engine/checks/custom.go)
_OUTPUT_START_RE = re.compile(r"\noutput:\n| couldn't find regex \".*?\" in ")
# Longest check output quoted in a report
REASON_CHARS = 150
# Consecutive rounds with every one of a team's checks failing before staff are alerted
BLACKOUT_ROUNDS = 3


def team_order(name: str) -> tuple[int, str]:
    """team2 before team10."""
    return len(name), name


def failure_line(service_type: str, debug: str) -> str:
    """The FAILURE: (or FAILED:) line a custom check printed; "" if there is none. Only a custom check's debug marks
    where its output starts, after the command line that holds the credentials; built-in checks have no such marker,
    and VNC puts the server's error ahead of the credentials. The last marker is used: a password can contain one,
    and only the check's output follows Quotient's own. Output cut short is skipped: the cut can drop Quotient's
    marker and leave one from a password last."""
    if service_type != "Custom" or debug.endswith(TRUNCATED):
        return ""
    markers = list(_OUTPUT_START_RE.finditer(debug))
    match = _FAILURE_RE.search(debug, markers[-1].end()) if markers else None
    reason = debug[match.end() :].strip() if match else ""
    return reason.splitlines()[0][:REASON_CHARS] if reason else ""


def preflight_report(preflight: PreflightRound | None, now: datetime) -> str:
    if preflight is None:
        return "**Preflight:** couldn't read a preflight round from Quotient."
    if not preflight.round_number or not preflight.start_time or now - preflight.start_time > PREFLIGHT_STALE:
        return (
            "**Preflight:** no preflight round in the last "
            f"{int(PREFLIGHT_STALE.total_seconds() // 60)} minutes. Is Quotient running, unpaused, "
            "with the competition not yet started?"
        )

    teams = {c.team_name for c in preflight.checks}
    failed = sorted(
        (c for c in preflight.checks if not c.passed), key=lambda c: (team_order(c.team_name), c.service_name)
    )
    at = f"<t:{int(preflight.start_time.timestamp())}:t>"
    if not failed:
        return f"**Preflight round {preflight.round_number}** ({at}): all {len(teams)} teams green."

    failing_teams = {c.team_name for c in failed}
    lines = [
        f"**Preflight round {preflight.round_number}** ({at}): "
        f"{len(teams) - len(failing_teams)}/{len(teams)} teams green. Failing:"
    ]
    # One check per line keeps each line short, so split_message breaks between lines, never inside a code span
    for check in failed:
        reason = failure_line(check.service_type, check.debug)
        quoted = f": {code_span(reason, REASON_CHARS + 2)}" if reason else ""
        lines.append(f"- **{check.team_name}** {check.service_name}{quoted}")
    return "\n".join(lines)


class BlackoutTracker:
    """Which of Quotient's team outages staff have been alerted to, so each is reported once."""

    def __init__(self) -> None:
        # team name -> when the outage they were alerted to began
        self.alerted: dict[str, datetime | None] = {}

    def reset(self) -> None:
        """Forget every alert, so outages still going on are reported again."""
        self.alerted.clear()

    def update(self, outages: list[TeamOutage], now: datetime) -> list[str]:
        """Messages for staff: teams that reached BLACKOUT_ROUNDS, and alerted teams that came back."""
        down = {o.team_name: o for o in outages if o.rounds_down >= BLACKOUT_ROUNDS}
        messages = []
        ended = [t for t, since in self.alerted.items() if t not in down or down[t].down_since != since]
        for team in sorted(ended, key=team_order):
            since = self.alerted.pop(team)
            lasted = f" after {int((now - since).total_seconds() // 60)} minutes" if since else ""
            messages.append(f"**{team}** is back up{lasted}.")
        for team in sorted(down, key=team_order):
            outage = down[team]
            if team in self.alerted:
                continue
            self.alerted[team] = outage.down_since
            started = f" since <t:{int(outage.down_since.timestamp())}:t>" if outage.down_since else ""
            messages.append(
                f"**{team}** has every service down for {outage.rounds_down} rounds{started}. "
                "Router, firewall or the team's network?"
            )
        return messages


class ScoringWatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.blackouts = BlackoutTracker()
        # (scheduled start, minutes before it) for each report time already posted or covered by a report
        self.reports_sent: set[tuple[datetime, int]] = set()

    async def cog_load(self) -> None:
        self.watch_loop.start()

    async def cog_unload(self) -> None:
        self.watch_loop.cancel()

    @tasks.loop(seconds=60)
    async def watch_loop(self) -> None:
        try:
            await recycle_db_connection()
            await self.watch()
        except Exception as e:
            logger.exception(f"Error watching Quotient: {e}")

    @watch_loop.before_loop
    async def before_watch(self) -> None:
        await self.bot.wait_until_ready()

    async def watch(self) -> None:
        config = await sync_to_async(CompetitionConfig.get_config)()
        now = timezone.now()
        start = config.competition_start_time

        if start and not config.applications_enabled and now < start:
            due = [m for m in PREFLIGHT_REPORT_MINUTES if now >= start - timedelta(minutes=m)]
            if due and (start, min(due)) not in self.reports_sent:
                self.reports_sent.update((start, m) for m in due)
                preflight = await sync_to_async(get_quotient_client().get_preflight)()
                for part in split_message(preflight_report(preflight, now)):
                    await log_to_ops_channel(self.bot, part)

        if not config.applications_enabled:
            self.blackouts.reset()
            return
        outages = await sync_to_async(get_quotient_client().get_outages)()
        if outages is None:
            return
        for message in self.blackouts.update(outages, now):
            await log_to_ops_channel(self.bot, message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScoringWatchCog(bot))
