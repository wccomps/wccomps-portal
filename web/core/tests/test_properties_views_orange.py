"""Property tests for the orange team views: the dashboard, check-ins, checks and their rubrics, assignments,
follow-ups, review and export. The properties are views_harness.check_answer's."""

from typing import Any

import pytest
from hypothesis import strategies as st
from orange_team.forms import AssignmentRejectForm, FollowUpForm, OrangeCheckForm, ReassignForm, SubmitScoreForm
from orange_team.models import OrangeAssignment, OrangeFollowUp

from core.tests.views_harness import (
    Fields,
    ViewCase,
    World,
    any_request_is_answered_safely,
    echo_sweep,
    form_fields,
    hostile_text,
    may,
    repeated_post_changes_nothing_more,
)

ORANGE = may("orange_team", "orange_team_lead")
LEAD = may("orange_team_lead")


def _check(w: World) -> Fields:
    """The check form and rubric rows, numbered as the page numbers them or not."""
    rows: Fields = {}
    for n in ("1", "2", "007", "١"):
        rows[f"criterion_label_{n}"] = hostile_text(200)
        rows[f"criterion_points_{n}"] = st.one_of(st.integers(0, 20).map(str), hostile_text())
        rows[f"criterion_id_{n}"] = st.one_of(st.sampled_from([str(c) for c in w.ids("criterion_id")]), hostile_text())
    return form_fields(OrangeCheckForm()) | rows


def _ids(w: World, kind: str) -> st.SearchStrategy[str]:
    return st.sampled_from([str(i) for i in w.ids(kind)])


def _form(form: Any, **hints: Any) -> Any:
    return lambda w: form_fields(form, {k: v(w) for k, v in hints.items()})


CASES = [
    ViewCase("orange_team:dashboard", ORANGE),
    ViewCase("orange_team:toggle_checkin", ORANGE),
    ViewCase("orange_team:admin_toggle_checkin", LEAD),
    ViewCase("orange_team:review_queue", LEAD),
    ViewCase("orange_team:check_list", LEAD),
    ViewCase("orange_team:auto_assign_all", LEAD),
    ViewCase("orange_team:check_create", LEAD, post=_check),
    ViewCase("orange_team:check_detail", LEAD),
    ViewCase("orange_team:check_edit", LEAD, post=_check),
    ViewCase("orange_team:check_duplicate", LEAD),
    ViewCase("orange_team:check_assign", LEAD),
    ViewCase("orange_team:check_auto_assign", LEAD, idempotent=(OrangeAssignment,)),
    ViewCase("orange_team:check_rebalance", LEAD),
    ViewCase(
        "orange_team:reassign_team",
        LEAD,
        post=_form(ReassignForm(), user_id=lambda w: _ids(w, "user_id")),
        idempotent=(OrangeAssignment,),
    ),
    ViewCase(
        "orange_team:assignment_save",
        ORANGE,
        json=True,
        json_body=True,
        methods=("POST",),
        post=lambda w: {"criterion_id": _ids(w, "criterion_id").map(int), "met": st.booleans()},
        idempotent=(OrangeAssignment,),
        idempotent_as="orange",
    ),
    ViewCase("orange_team:assignment_submit", ORANGE, post=_form(SubmitScoreForm())),
    ViewCase("orange_team:assignment_approve", LEAD),
    ViewCase("orange_team:assignment_reject", LEAD, post=_form(AssignmentRejectForm())),
    ViewCase("orange_team:export_scores", LEAD),
    ViewCase(
        "orange_team:followup_create",
        ORANGE,
        post=_form(FollowUpForm(), assignment_id=lambda w: _ids(w, "assignment_id")),
    ),
    ViewCase("orange_team:followup_dismiss", ORANGE, idempotent=(OrangeFollowUp,), idempotent_as="orange"),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_any_request_is_answered_safely(case: ViewCase, view_world: World) -> None:
    any_request_is_answered_safely(case, view_world)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_every_field_is_escaped_where_echoed(case: ViewCase, view_world: World) -> None:
    echo_sweep(case, view_world)


IDEMPOTENT = [case for case in CASES if case.idempotent]


@pytest.mark.parametrize("case", IDEMPOTENT, ids=[c.name for c in IDEMPOTENT])
def test_repeated_post_changes_nothing_more(case: ViewCase, view_world: World) -> None:
    repeated_post_changes_nothing_more(case, view_world)
