"""The view property rows for the registration views."""

import pytest
from hypothesis import strategies as st
from registration.forms import EventForm, RegistrationForm, RejectRegistrationForm, SeasonForm, ShownSchoolForm
from registration.models import Event, Season, TeamRegistration

from core.tests.views_harness import (
    EVERYONE,
    Fields,
    ViewCase,
    World,
    any_request_is_answered_safely,
    echo_sweep,
    every_allowed_role_gets_in,
    form_fields,
    hostile_text,
    may,
    repeated_post_changes_nothing_more,
)

GOLD = may("gold_team")


def _registration(w: World) -> Fields:
    return form_fields(RegistrationForm(), {"region": st.sampled_from(["wrccdc", "prccdc", "x"])})


CASES = [
    ViewCase("registration_register", EVERYONE, post=_registration),
    ViewCase("registration_edit", EVERYONE, params={"token": "edit_token"}, post=_registration),
    ViewCase(
        "registration_review_list",
        GOLD,
        query=lambda w: {"status": st.one_of(st.sampled_from(["pending", "approved", "rejected"]), hostile_text())},
    ),
    ViewCase("registration_approve", GOLD, idempotent=(TeamRegistration,)),
    ViewCase("registration_reject", GOLD, post=lambda w: form_fields(RejectRegistrationForm())),
    ViewCase("registration_season_list", GOLD),
    ViewCase("registration_season_create", GOLD, post=lambda w: form_fields(SeasonForm())),
    ViewCase("registration_season_edit", GOLD, post=lambda w: form_fields(SeasonForm()), idempotent=(Season,)),
    ViewCase("registration_season_delete", GOLD),
    ViewCase("registration_event_list", GOLD),
    ViewCase("registration_event_create", GOLD, post=lambda w: form_fields(EventForm())),
    ViewCase("registration_event_detail", GOLD),
    ViewCase("registration_event_edit", GOLD, post=lambda w: form_fields(EventForm()), idempotent=(Event,)),
    ViewCase("registration_event_delete", GOLD),
    ViewCase("registration_assign_teams", GOLD),
    ViewCase(
        "registration_unassign_team",
        GOLD,
        params={"assignment_id": "event_assignment_id"},
        post=lambda w: form_fields(ShownSchoolForm()),
    ),
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
