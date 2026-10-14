"""Unfinished work shown before a close-out: each kind with its count, and whether the scorecards went out as they
stand now. Warnings never block."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from orange_team.models import OrangeAssignment, OrangeCheck
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore, ServiceScore
from scoring.scorecard import build_scorecard_context

from core.models import AuditLog
from registration.models import Event, Season
from registration.services import close_out_event, close_out_warnings, scorecards_fingerprint, set_team_school
from team.models import Team
from ticketing.models import Ticket

pytestmark = pytest.mark.django_db

NOT_EMAILED = "Scorecards haven't been emailed for this event."


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
    for number, points in ((1, 300), (2, 200)):
        team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
        ServiceScore.objects.create(team=team, service_points=Decimal(points))
        set_team_school(team, f"School {number}", contact_email=f"c{number}@example.edu")
    return event


def _emailed(event, **details):
    AuditLog.objects.create(
        action="scorecards_emailed",
        admin_user="gold",
        target_entity="event",
        target_id=event.pk if event else 0,
        details={"event": event.pk if event else None, "teams": 2, "failed": 0, **details},
    )


def _team(number=1):
    return Team.objects.get(team_number=number)


def test_finished_work_emailed_and_unchanged_has_no_warnings(event):
    _emailed(event, scorecards_hash=scorecards_fingerprint(event))

    assert close_out_warnings(event) == []


def test_each_kind_of_unfinished_work_is_counted(event):
    user = User.objects.create_user("grader")
    for inject in ("1", "2", "3"):
        InjectScore.objects.create(team=_team(), inject_id=inject, inject_name="Memo", points_awarded=Decimal(5))
    InjectScore.objects.create(
        team=_team(2), inject_id="1", inject_name="Memo", points_awarded=Decimal(5), is_approved=True
    )
    for number, status in enumerate(("open", "claimed", "resolved", "resolved", "cancelled"), start=1):
        Ticket.objects.create(
            ticket_number=f"T001-00{number}", team=_team(), title="Help", status=status, is_approved=number == 4
        )
    for _ in range(2):
        IncidentReport.objects.create(
            team=_team(), attack_description="scan", source_ip="10.0.0.1", attack_detected_at="2026-10-03T10:00Z"
        )
    RedTeamScore.objects.create(attack_vector="ssh", points_per_team=Decimal(10), submitted_by=user)
    OrangeTeamScore.objects.create(team=_team(), description="Call", points_awarded=Decimal(5))
    check = OrangeCheck.objects.create(title="Phone call", created_by=user)
    for team, status in ((_team(1), "pending"), (_team(2), "submitted")):
        OrangeAssignment.objects.create(orange_check=check, user=user, team=team, status=status)
    _emailed(event, scorecards_hash=scorecards_fingerprint(event))

    assert close_out_warnings(event) == [
        "3 inject grades are not approved.",
        "2 tickets are not resolved.",
        "1 resolved ticket is not approved.",
        "2 incident reports are not reviewed.",
        "1 red team finding is not approved.",
        "1 orange check score is not approved.",
        "2 orange assignments are not scored and approved.",
    ]


def test_scorecards_never_emailed(event):
    assert close_out_warnings(event) == [NOT_EMAILED]


def test_scorecards_emailed_for_another_event_do_not_count(event):
    other = Event.objects.create(season=event.season, name="Earlier", event_type="qualifier", date=date(2026, 9, 1))
    _emailed(other, scorecards_hash=scorecards_fingerprint(event))

    assert close_out_warnings(event) == [NOT_EMAILED]


def test_scores_changed_after_the_last_scorecard_email(event):
    _emailed(event, scorecards_hash=scorecards_fingerprint(event))
    ServiceScore.objects.filter(team__team_number=2).update(service_points=Decimal(400))

    assert close_out_warnings(event) == ["Scores changed after the last scorecard email."]


def test_the_latest_scorecard_email_decides(event):
    stale = scorecards_fingerprint(event)
    ServiceScore.objects.filter(team__team_number=2).update(service_points=Decimal(400))
    _emailed(event, scorecards_hash=stale)
    _emailed(event, scorecards_hash=scorecards_fingerprint(event))

    assert close_out_warnings(event) == []


def test_failed_scorecard_emails_are_counted(event):
    _emailed(event, scorecards_hash=scorecards_fingerprint(event), failed=1)

    assert close_out_warnings(event) == ["1 scorecard email failed in the last send."]


def test_the_fingerprint_is_stable_with_averaged_decimals_and_int_keys(event):
    """JSON turns int keys into strings and Decimals into tagged values; the same scorecards hash the same."""

    def with_int_keys(score, standings):
        context = build_scorecard_context(score, standings)
        by_team = {score.team.team_number: Decimal("0.3333333333"), "all": Decimal("0.5")}
        context["stats"] = {**context["stats"], "by_team": by_team}
        return context

    with patch("registration.services.build_scorecard_context", with_int_keys):
        assert scorecards_fingerprint(event) == scorecards_fingerprint(event)


def test_with_no_event_open_the_school_list_decides(event):
    Event.objects.filter(pk=event.pk).update(is_active=False)
    _emailed(None, scorecards_hash=scorecards_fingerprint(None))

    assert scorecards_fingerprint(None) == scorecards_fingerprint(event)
    assert close_out_warnings(None) == []
    set_team_school(_team(2), "Example University", contact_email="c@example.edu")
    assert close_out_warnings(None) == ["Scores changed after the last scorecard email."]


def test_an_open_event_with_no_live_scores_says_its_results_will_be_empty(event):
    ServiceScore.objects.all().delete()
    _emailed(event, scorecards_hash=scorecards_fingerprint(event))

    assert close_out_warnings(event) == ["No team has live scores yet, so the results will be empty."]


def test_warnings_never_block_a_close_out(event):
    InjectScore.objects.create(team=_team(), inject_id="1", inject_name="Memo", points_awarded=Decimal(5))

    assert close_out_warnings(event)
    assert close_out_event(event, "gold").results == 2
