"""The preflight report, from engine statuses shaped as Quotient's GET /api/engine answers them."""

import copy
from datetime import UTC, datetime, timedelta

from quotient.client import _engine_status

from scoring.preflight import preflight_report

ROUND_START = datetime(2026, 11, 21, 16, 30, tzinfo=UTC)
NOW = ROUND_START + timedelta(minutes=1)
ZERO_ROUND = {"ID": 0, "StartTime": "0001-01-01T00:00:00Z", "Checks": None, "SLAs": None}


def _check(team_id: int, service: str, result: bool, error: str = "", debug: str = "") -> dict[str, object]:
    return {
        "TeamID": team_id,
        "RoundID": 4,
        "Round": ZERO_ROUND,
        "ServiceName": service,
        "Points": 5,
        "Result": result,
        "Error": error,
        "Debug": debug,
    }


def _engine(checks: list[dict[str, object]] | None, **changes: object) -> dict[str, object]:
    """GET /api/engine during preflight on Quotient main: the latest preflight round as last_round."""
    answer: dict[str, object] = {
        "last_round": {"ID": 4, "StartTime": "2026-11-21T16:30:00Z", "Checks": checks, "SLAs": None},
        "current_round_time": "2026-11-21T16:30:00Z",
        "next_round_time": "2026-11-21T16:31:00Z",
        "running": True,
        "competition_started": False,
        "scheduled_start_time": "2026-11-21T17:00:00Z",
        "scheduled_end_time": "2026-11-22T01:00:00Z",
        "schedule_source": "config",
        "preflight": True,
        "competition_timezone": "America/Los_Angeles",
    }
    answer.update(changes)
    return answer


NAMES = {1: "team1", 2: "team2", 10: "team10"}


def test_lists_each_failing_team_once_with_its_failed_services_in_team_order():
    status = _engine_status(
        _engine(
            [
                _check(10, "web-http", False),
                _check(2, "ssh", False),
                _check(2, "dns", False),
                _check(1, "web-http", True),
            ]
        )
    )

    report = preflight_report(status, NAMES, NOW)

    assert report.splitlines() == [
        "**Preflight round 4** (<t:1795278600:t>): 1/3 teams green. Failing:",
        "- **team2**: dns, ssh",
        "- **team10**: web-http",
    ]


def test_all_green():
    status = _engine_status(_engine([_check(1, "web-http", True), _check(2, "web-http", True)]))

    assert preflight_report(status, NAMES, NOW).endswith("all 2 teams green.")


def test_never_quotes_a_checks_error_or_debug_output():
    """A built-in check's error names the credentials it tried, and debug output comes from the team's box."""
    status = _engine_status(
        _engine([_check(1, "ssh", False, "error logging in to ssh server for creds root:hunter2", "HTTP 503 <@123>")])
    )

    report = preflight_report(status, NAMES, NOW)

    assert "hunter2" not in report
    assert "<@123>" not in report
    assert "- **team1**: ssh" in report


def test_a_team_without_a_name_shows_its_id():
    status = _engine_status(_engine([_check(7, "web-http", False)]))

    assert "- **team ID 7**: web-http" in preflight_report(status, NAMES, NOW)
    assert "- **team ID 7**: web-http" in preflight_report(status, None, NOW)


def test_a_quotient_build_without_the_preflight_field_says_it_cant_read_preflight():
    """A Quotient build from before preflight moved into GET /api/engine answers with no preflight field; its
    last_round is the last scored round, so reporting it would be wrong."""
    answer = _engine([_check(1, "web-http", False)])
    del answer["preflight"]

    report = preflight_report(_engine_status(answer), NAMES, NOW)

    assert "can't read a preflight round" in report
    assert "team1" not in report


def test_a_scored_or_started_competition_has_no_preflight_round_to_report():
    status = _engine_status(_engine([_check(1, "web-http", False)], preflight=False))

    report = preflight_report(status, NAMES, NOW)

    assert "runs no preflight rounds" in report
    assert "team1" not in report


def test_no_preflight_round_yet():
    status = _engine_status(_engine(None, last_round=copy.deepcopy(ZERO_ROUND)))

    assert "no preflight round in the last 10 minutes" in preflight_report(status, NAMES, NOW)


def test_a_stale_round_from_a_paused_engine_says_it_is_paused():
    status = _engine_status(_engine([_check(1, "web-http", False)], running=False))

    report = preflight_report(status, NAMES, ROUND_START + timedelta(minutes=11))

    assert "no preflight round in the last 10 minutes" in report
    assert "paused" in report
    assert "team1" not in report


def test_a_round_that_ran_no_checks():
    status = _engine_status(_engine([]))

    assert "ran no checks" in preflight_report(status, NAMES, NOW)


def test_quotient_unreadable():
    assert "couldn't read the engine status" in preflight_report(None, None, NOW)
