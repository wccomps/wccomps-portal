"""The view property rows for the scoring views."""

from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import strategies as st
from scoring.forms import (
    ApproveInjectFeedbackForm,
    IncidentMatchForm,
    IncidentReportForm,
    InjectGradingForm,
    RedTeamIPPoolForm,
    RedTeamScoreForm,
    SaveInjectFeedbackForm,
    ScorecardEmailForm,
    ScoringTemplateForm,
)
from scoring.models import IncidentReport, InjectScore, OrangeTeamScore, RedTeamScore, ScoringTemplate

from core.tests.strategies import rolled_back
from core.tests.views_harness import (
    SIGNED_IN,
    Fields,
    ViewCase,
    World,
    any_request_is_answered_safely,
    build,
    check_answer,
    echo_sweep,
    every_allowed_role_gets_in,
    form_fields,
    hostile_text,
    may,
    repeated_post_changes_nothing_more,
    send,
    uploads,
)

STAFF = may("gold_team", "white_team", "red_team", "ticketing_admin", "orange_team", "ticketing_support")
GOLD = may("gold_team")
RED = may("red_team")
GOLD_WHITE = may("gold_team", "white_team")
FILTERS = (
    "status", "team", "attack_type", "submitter", "search", "box", "check", "inject", "outliers", "sort", "page",
)  # fmt: skip


def _filters(w: World) -> Fields:
    ids = st.sampled_from([str(i) for i in w.ids("team_id") + w.ids("user_id") + w.ids("check_id")])
    return {name: st.one_of(ids, st.sampled_from(["pending", "approved", "all", "manual", "1"]), hostile_text())
            for name in FILTERS}  # fmt: skip


def _ids(w: World, kind: str) -> st.SearchStrategy[Any]:
    return st.one_of(st.lists(st.sampled_from([str(i) for i in w.ids(kind)]), min_size=1), hostile_text())


def _red_score(w: World) -> Fields:
    form = RedTeamScoreForm(team_count=3, user=w.users["red_team"])
    return form_fields(form) | {
        "screenshots": uploads(3),
        "attack_vector": hostile_text(),
        "target_teams": hostile_text(),
    }


def _incident(w: World) -> Fields:
    return form_fields(IncidentReportForm(w.teams[0], True)) | {"screenshots": uploads(3)}


def _match(w: World) -> Fields:
    incident = IncidentReport.objects.get(pk=w.ids("incident_id")[0])
    return form_fields(IncidentMatchForm([], instance=incident))


def _grading(w: World) -> Fields:
    return form_fields(InjectGradingForm(team_numbers=w.ids("team_number")), {"inject_id": st.just("1")})


def _injects(w: World) -> ExitStack:
    """Quotient lists one inject, so the grading page has one to grade."""
    stack = ExitStack()
    inject = SimpleNamespace(inject_id=1, title="Inject 01 - Web (100 pts)")
    stack.enter_context(patch("quotient.client.QuotientClient.get_injects", return_value=[inject]))
    return stack


def _feedback(form: Any) -> Any:
    return lambda w: form_fields(form, {"score_id": st.sampled_from([str(g) for g in w.ids("grade_id")])})


EXPORTS = [
    ViewCase(
        f"scoring:export_{dataset}",
        GOLD,
        query=lambda w: {"format": st.sampled_from(["csv", "json", "CSV"])},
        json_sometimes="answers JSON for ?format=json and CSV otherwise; it is a download, not a JSON endpoint",
    )
    for dataset in ("red_scores", "incidents", "orange_adjustments", "inject_grades", "final_scores", "tickets")
]

CASES = [
    ViewCase("leaderboard_page", STAFF, query=lambda w: {"schools": hostile_text(), "names": hostile_text()}),
    ViewCase("leaderboard_scorecard", STAFF),
    ViewCase("leaderboard_scorecard_pdf", STAFF, methods=("GET",)),
    ViewCase("scoring:leaderboard", STAFF, query=lambda w: {"schools": hostile_text(), "names": hostile_text()}),
    ViewCase("scoring:scorecard", STAFF),
    ViewCase("scoring:scorecard_pdf", STAFF, methods=("GET",)),
    ViewCase("scoring:submit_red_score", RED, post=_red_score),
    ViewCase("scoring:red_team_scores", RED, query=_filters),
    ViewCase("scoring:view_red_score", may("red_team", "gold_team")),
    ViewCase("scoring:red_team_findings", may("red_team", "gold_team"), query=_filters),
    ViewCase("scoring:delete_red_score", RED),
    ViewCase("scoring:leave_red_score", RED),
    ViewCase(
        "scoring:bulk_approve_red_scores",
        GOLD,
        post=lambda w: {"finding_ids": _ids(w, "finding_id")},
        idempotent=(RedTeamScore,),
    ),
    ViewCase("scoring:red_screenshot", may("red_team", "gold_team"), params={"screenshot_id": "red_screenshot_id"}),
    ViewCase("scoring:ip_pool_list", RED),
    ViewCase(
        "scoring:ip_pool_create",
        RED,
        post=lambda w: form_fields(RedTeamIPPoolForm(user=w.users["red_team"])),
        json_sometimes="answers the finding form's pool modal (an XHR) with JSON, and its own form page otherwise",
    ),
    ViewCase("scoring:ip_pool_edit", RED, post=lambda w: form_fields(RedTeamIPPoolForm(user=w.users["red_team"]))),
    ViewCase("scoring:ip_pool_delete", RED),
    ViewCase("scoring:incident_list", GOLD_WHITE | {"blue_team"}),
    ViewCase("scoring:submit_incident_report", GOLD_WHITE | {"blue_team"}, post=_incident),
    ViewCase("scoring:view_incident_report", GOLD_WHITE | {"blue_team"}),
    ViewCase("scoring:delete_incident_report", GOLD_WHITE | {"blue_team"}),
    ViewCase(
        "scoring:incident_screenshot", GOLD_WHITE | {"blue_team"}, params={"screenshot_id": "incident_screenshot_id"}
    ),
    ViewCase("scoring:orange_team_redirect", SIGNED_IN),
    ViewCase("scoring:submit_orange_check", SIGNED_IN),
    ViewCase(
        "scoring:bulk_approve_orange_adjustments",
        may("orange_team"),
        post=lambda w: {"adjustment_ids": _ids(w, "orange_score_id")},
        idempotent=(OrangeTeamScore,),
    ),
    ViewCase(
        "scoring:inject_grading",
        GOLD_WHITE,
        query=lambda w: {"inject": st.one_of(st.just("1"), hostile_text())},
        post=_grading,
        patches=_injects,
        idempotent=(InjectScore,),
    ),  # fmt: skip
    ViewCase("scoring:inject_grades_review", GOLD_WHITE, query=_filters),
    ViewCase(
        "scoring:inject_grades_bulk_approve",
        GOLD_WHITE,
        post=lambda w: {"grade_ids": _ids(w, "grade_id")},
        idempotent=(InjectScore,),
    ),
    ViewCase("scoring:review_incidents", GOLD_WHITE, query=_filters),
    ViewCase(
        "scoring:bulk_approve_incidents",
        GOLD_WHITE,
        post=lambda w: {"incident_ids": _ids(w, "incident_id")},
        idempotent=(IncidentReport,),
    ),
    ViewCase("scoring:match_incident", GOLD_WHITE, post=_match),
    ViewCase("scoring:review_orange", may("orange_team"), query=_filters),
    ViewCase("scoring:review_inject_feedback", GOLD_WHITE, query=_filters),
    ViewCase(
        "scoring:save_inject_feedback", GOLD_WHITE, post=_feedback(SaveInjectFeedbackForm()), idempotent=(InjectScore,)
    ),
    ViewCase(
        "scoring:approve_inject_feedback",
        GOLD_WHITE,
        post=_feedback(ApproveInjectFeedbackForm()),
        idempotent=(InjectScore,),
    ),
    ViewCase(
        "scoring:bulk_approve_inject_feedback",
        GOLD_WHITE,
        post=lambda w: {"score_ids": _ids(w, "grade_id")},
        idempotent=(InjectScore,),
    ),
    ViewCase(
        "scoring:scoring_config",
        GOLD,
        post=lambda w: form_fields(ScoringTemplateForm(instance=ScoringTemplate.objects.first())),
        idempotent=(ScoringTemplate,),
    ),
    ViewCase("scoring:sync_scores", GOLD),
    ViewCase("scoring:api_scores", may("gold_team", "white_team", "ticketing_admin"), json=True),
    ViewCase("scoring:export_index", GOLD),
    ViewCase("scoring:export_all", GOLD, methods=("GET",)),
    *EXPORTS,
    ViewCase("scoring:export_scorecards", GOLD, methods=("GET",)),
    ViewCase("scoring:email_scorecards", GOLD),
    ViewCase("scoring:stream_email_scorecards", GOLD, post=lambda w: form_fields(ScorecardEmailForm())),
    ViewCase("scoring:email_scorecard", GOLD, post=lambda w: form_fields(ScorecardEmailForm())),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_any_request_is_answered_safely(case: ViewCase, view_world: World) -> None:
    any_request_is_answered_safely(case, view_world)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_every_field_is_escaped_where_echoed(case: ViewCase, view_world: World) -> None:
    echo_sweep(case, view_world)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_every_allowed_role_gets_in(case: ViewCase, view_world: World) -> None:
    every_allowed_role_gets_in(case, view_world)


IDEMPOTENT = [case for case in CASES if case.idempotent]


@pytest.mark.parametrize("case", IDEMPOTENT, ids=[c.name for c in IDEMPOTENT])
def test_repeated_post_changes_nothing_more(case: ViewCase, view_world: World) -> None:
    repeated_post_changes_nothing_more(case, view_world)


@pytest.mark.parametrize("case", EXPORTS, ids=[c.name for c in EXPORTS])
def test_an_export_answers_json_for_format_json(case: ViewCase, view_world: World) -> None:
    """The one view that reads a format parameter: ?format=json is its JSON answer (CI drew it first)."""
    drawn = build(case, role="admin", method="GET", name=case.name, kwargs={}, query={"format": ["json"]}, fields={})
    with rolled_back():
        check_answer(case, drawn, send(view_world, drawn))
