"""The preflight report and blackout alerts staff get from the scoring watch."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st
from quotient.client import MAX_CHECK_OUTPUT_CHARS, PreflightCheck, PreflightRound, TeamOutage, _clean_output

from bot.cogs import scoring_watch
from bot.cogs.scoring_watch import (
    BLACKOUT_ROUNDS,
    BlackoutTracker,
    ScoringWatchCog,
    failure_line,
    preflight_report,
)
from bot.utils import split_message

NOW = datetime(2026, 10, 31, 15, 40, tzinfo=UTC)


def check(team: str, service: str, passed: bool, output: str = "", service_type: str = "Custom") -> PreflightCheck:
    """A check result whose debug is laid out as Quotient writes a custom check's: the command, then its output."""
    debug = f"python3 /app/checks/x.py 10.100.101.5 jdoe 'Secret!1'\noutput:\n{output}" if output else ""
    return PreflightCheck(
        team_name=team, service_name=service, service_type=service_type, passed=passed, error="", debug=debug
    )


def preflight(*checks: PreflightCheck, at: datetime = NOW - timedelta(minutes=1)) -> PreflightRound:
    return PreflightRound(round_number=7, start_time=at, checks=list(checks))


def test_report_all_green() -> None:
    report = preflight_report(preflight(check("team01", "web", True), check("team02", "web", True)), NOW)
    assert "all 2 teams green" in report


def test_report_lists_failing_teams_with_their_check_output() -> None:
    debug = "FAILURE: Keycloak at 10.100.114.122 rejected jdoe"
    report = preflight_report(
        preflight(
            check("team02", "web", True),
            check("team14", "wiki", False, debug),
            check("team14", "chat", False),
            check("team03", "web", True),
        ),
        NOW,
    )
    assert "2/3 teams green" in report
    assert "- **team14** chat\n- **team14** wiki: `Keycloak at 10.100.114.122 rejected jdoe`" in report
    assert "Secret!1" not in report


def test_report_shows_team_text_without_markdown() -> None:
    debug = "FAILURE: [reset here](https://evil.example) `x` **all green**"
    report = preflight_report(preflight(check("team01", "web", False, debug)), NOW)
    reason = report.split("web: ", 1)[1]
    assert "\n" not in reason
    assert reason.startswith("`") and reason.endswith("`")
    assert reason.count("`") == 2, "a backtick in team text can't end the code span"


@given(reasons=st.lists(st.text(min_size=1), min_size=1, max_size=60))
def test_no_message_part_cuts_a_code_span(reasons: list[str]) -> None:
    """However many checks fail with whatever text, every part posted has whole code spans, so team text never
    renders as markdown."""
    checks = [check("team07", f"svc{i:02d}", False, f"FAILURE: {r}") for i, r in enumerate(reasons)]
    for part in split_message(preflight_report(preflight(*checks), NOW)):
        assert len(part) <= 2000
        assert part.count("`") % 2 == 0, part


def test_report_flags_missing_or_stale_preflight() -> None:
    assert "couldn't read a preflight round" in preflight_report(None, NOW)
    assert "no preflight round" in preflight_report(PreflightRound(0, None, []), NOW)
    stale = preflight(check("team01", "web", True), at=NOW - scoring_watch.PREFLIGHT_STALE - timedelta(minutes=1))
    assert "no preflight round" in preflight_report(stale, NOW)


@given(
    command=st.text() | st.just("check.py 10.0.0.5 jdoe 'x\noutput:\nFAILURE: secret'"),
    marker=st.sampled_from(["FAILURE:", "FAILED:"]),
    reason=st.text().filter(lambda r: "\noutput:\n" not in r and "couldn't find regex" not in r),
)
def test_failure_line_reads_only_a_custom_checks_output(command: str, marker: str, reason: str) -> None:
    """Only what a custom check printed after Quotient's output marker is quoted, never its command line (which holds
    the credentials), even when the command itself contains a failure marker."""
    debug = f"{command} FAILURE: in-command\noutput:\n{marker}{reason}"
    assert failure_line("Custom", debug) == (reason.strip().splitlines()[0][:150] if reason.strip() else "")


def test_failure_line_reads_a_regex_mismatch() -> None:
    debug = "python3 check.py 10.0.0.5 jdoe 'pw' couldn't find regex \"CORRECT\" in FAILURE: login failed (HTTP 503)"
    assert failure_line("Custom", debug) == "login failed (HTTP 503)"


def test_failure_line_skips_output_cut_short() -> None:
    """A password padded past the client's cap, ending in a fake marker, must not make the command line quotable."""
    password = "p" * (MAX_CHECK_OUTPUT_CHARS - 200) + "\noutput:\nFAILURE: "
    raw = f"check.py 10.0.0.5 jdoe '{password}' --token {'S' * 500}\noutput:\nFAILURE: real reason"
    debug = _clean_output(raw, "debug")
    assert "real reason" not in debug, "the cut drops Quotient's own marker"
    assert failure_line("Custom", debug) == ""


def test_failure_line_ignores_built_in_checks() -> None:
    """A built-in check can write a server's text before credentials (VNC: "<error> for creds user:pass")."""
    debug = "\noutput:\nFAILURE: hi for creds jdoe:Secret!1"
    assert failure_line("Vnc", debug) == ""
    assert failure_line("", debug) == ""


def outage(team: str, rounds: int, since_minutes: int = 30) -> TeamOutage:
    return TeamOutage(team_name=team, rounds_down=rounds, down_since=NOW - timedelta(minutes=since_minutes))


def test_blackout_alerts_once_at_the_threshold_and_on_recovery() -> None:
    tracker = BlackoutTracker()
    assert tracker.update([outage("team16", BLACKOUT_ROUNDS - 1)], NOW) == []

    alert = tracker.update([outage("team16", BLACKOUT_ROUNDS), outage("team02", 1)], NOW)
    assert len(alert) == 1
    assert "**team16** has every service down for 3 rounds" in alert[0]
    assert tracker.update([outage("team16", BLACKOUT_ROUNDS + 5)], NOW) == []

    assert tracker.update([], NOW) == ["**team16** is back up after 30 minutes."]
    assert tracker.update([], NOW) == []


def test_an_outage_that_starts_over_is_reported_as_recovered_then_alerted() -> None:
    tracker = BlackoutTracker()
    tracker.update([outage("team16", BLACKOUT_ROUNDS, since_minutes=60)], NOW)
    messages = tracker.update([outage("team16", BLACKOUT_ROUNDS, since_minutes=10)], NOW)
    assert messages[0] == "**team16** is back up after 60 minutes."
    assert "has every service down" in messages[1]


@pytest.fixture
def cog() -> ScoringWatchCog:
    cog = ScoringWatchCog.__new__(ScoringWatchCog)
    cog.bot = MagicMock()
    cog.blackouts = BlackoutTracker()
    cog.reports_sent = set()
    return cog


def config(start: datetime | None, enabled: bool = False) -> MagicMock:
    return MagicMock(competition_start_time=start, applications_enabled=enabled)


async def run_at(cog: ScoringWatchCog, now: datetime, conf: MagicMock, client: MagicMock) -> AsyncMock:
    log = AsyncMock()
    with (
        patch.object(scoring_watch.CompetitionConfig, "get_config", return_value=conf),
        patch.object(scoring_watch.timezone, "now", return_value=now),
        patch.object(scoring_watch, "get_quotient_client", return_value=client),
        patch.object(scoring_watch, "log_to_ops_channel", log),
    ):
        await cog.watch()
    return log


@pytest.mark.asyncio
async def test_preflight_report_posts_at_thirty_and_ten_minutes(cog: ScoringWatchCog) -> None:
    start = NOW + timedelta(hours=1)
    client = MagicMock()
    client.get_preflight.return_value = preflight(check("team01", "web", True), at=start - timedelta(minutes=31))
    posted = []
    for minutes in (45, 30, 29, 20, 10, 9, 1):
        log = await run_at(cog, start - timedelta(minutes=minutes), config(start), client)
        posted.append(log.await_count)
    assert posted == [0, 1, 0, 0, 1, 0, 0]


@pytest.mark.asyncio
async def test_a_long_preflight_report_is_posted_in_full(cog: ScoringWatchCog) -> None:
    start = NOW + timedelta(minutes=30)
    debug = "FAILURE: " + "x" * 140
    checks = [check(f"team{n:02d}", f"box{s}-svc", False, debug) for n in range(1, 39) for s in range(3)]
    client = MagicMock()
    client.get_preflight.return_value = preflight(*checks, at=start - timedelta(minutes=1))
    log = await run_at(cog, start - timedelta(minutes=30), config(start), client)

    parts = [c.args[1] for c in log.await_args_list]
    assert len(parts) > 1
    assert all(len(p) <= 2000 for p in parts)
    assert "\n".join(parts) == preflight_report(client.get_preflight.return_value, start - timedelta(minutes=30))


@pytest.mark.asyncio
async def test_preflight_report_after_a_restart_posts_once(cog: ScoringWatchCog) -> None:
    start = NOW + timedelta(hours=1)
    client = MagicMock()
    client.get_preflight.return_value = None
    first = await run_at(cog, start - timedelta(minutes=5), config(start), client)
    second = await run_at(cog, start - timedelta(minutes=4), config(start), client)
    assert (first.await_count, second.await_count) == (1, 0)


@pytest.mark.asyncio
async def test_blackouts_are_watched_only_while_the_competition_runs(cog: ScoringWatchCog) -> None:
    client = MagicMock()
    client.get_outages.return_value = [outage("team16", BLACKOUT_ROUNDS)]
    first = await run_at(cog, NOW, config(None, enabled=True), client)
    again = await run_at(cog, NOW, config(None, enabled=True), client)
    stopped = await run_at(cog, NOW, config(None, enabled=False), client)
    restarted = await run_at(cog, NOW, config(None, enabled=True), client)

    assert (first.await_count, again.await_count, stopped.await_count) == (1, 0, 0)
    assert client.get_outages.call_count == 3, "nothing is read from Quotient while the competition is off"
    assert restarted.await_count == 1, "a stop forgets which outages were alerted"
