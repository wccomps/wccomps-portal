"""Property tests for the ticketing views: the list, detail and its actions, attachments, bulk actions, creation,
notifications and the ops review. The properties are views_harness.check_answer's."""

from typing import Any

import pytest
from hypothesis import strategies as st

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
    uploads,
)
from ticketing.forms import (
    CreateTicketForm,
    TicketBulkActionForm,
    TicketChangeCategoryForm,
    TicketCommentForm,
    TicketReassignForm,
    TicketReopenForm,
    TicketResolveForm,
)
from ticketing.models import Ticket, TicketHistory

# Ticket pages are for ticketing staff and for teams (their own tickets); the view refuses everyone else
TICKET_USERS = may("ticketing_support") | {"blue"}
STAFF = may("ticketing_support")
FILTERS = ("status", "category", "search", "team", "assignee", "page_size", "sort", "page")


def _filters(w: World) -> Fields:
    numbers = st.sampled_from([str(n) for n in w.ids("team_number") + w.ids("category_id")])
    return {name: st.one_of(numbers, hostile_text()) for name in FILTERS}


def _ticket_numbers(w: World) -> st.SearchStrategy[str]:
    return st.lists(st.sampled_from(w.ids("ticket_number")), min_size=1).map(",".join)


def _form(form: Any, **hints: st.SearchStrategy[Any]) -> Any:
    return lambda w: form_fields(form, hints)


CASES = [
    ViewCase("ticket_list", TICKET_USERS, query=_filters),
    ViewCase(
        "create_ticket",
        TICKET_USERS | may("gold_team", "admin", "ticketing_admin"),
        post=lambda w: form_fields(
            CreateTicketForm(),
            {
                "category": st.sampled_from([str(c) for c in w.ids("category_id")]),
                "team_id": st.sampled_from([str(t) for t in w.ids("team_id")]),
            },
        ),  # fmt: skip
    ),
    ViewCase(
        "tickets_bulk_claim",
        STAFF,
        post=lambda w: form_fields(TicketBulkActionForm(), {"ticket_numbers": _ticket_numbers(w)}),
        idempotent=(Ticket,),
        idempotent_as="support",
    ),
    ViewCase(
        "tickets_bulk_resolve",
        STAFF,
        post=lambda w: form_fields(TicketBulkActionForm(), {"ticket_numbers": _ticket_numbers(w)}),
        idempotent=(Ticket,),
        idempotent_as="ticketing_admin",
    ),
    ViewCase("ticket_notifications", STAFF, json=True, query=lambda w: {"since_id": hostile_text()}),
    ViewCase("ticket_detail", TICKET_USERS, query=_filters),
    ViewCase(
        "ticket_detail_dynamic",
        TICKET_USERS,
        query=lambda w: {"comments_version": hostile_text(), "history_version": hostile_text()},
    ),
    ViewCase("ticket_comment", TICKET_USERS, post=_form(TicketCommentForm())),
    ViewCase("ticket_cancel", frozenset({"blue"})),
    ViewCase("ticket_claim", STAFF, idempotent=(Ticket,), idempotent_as="support"),
    ViewCase("ticket_unclaim", STAFF),
    ViewCase(
        "ticket_reassign",
        STAFF,
        post=_form(TicketReassignForm(), new_assignee_username=st.sampled_from(["prop-support", "prop-admin"])),
    ),
    ViewCase("ticket_resolve", STAFF, post=_form(TicketResolveForm())),
    ViewCase("ticket_reopen", STAFF, post=_form(TicketReopenForm())),
    ViewCase(
        "ticket_change_category",
        STAFF,
        post=lambda w: form_fields(
            TicketChangeCategoryForm(), {"new_category": st.sampled_from([str(c) for c in w.ids("category_id")])}
        ),
    ),
    ViewCase("ticket_attachment_upload", TICKET_USERS, post=lambda w: {"attachment": uploads()}),
    ViewCase("ticket_attachment_download", TICKET_USERS, query=lambda w: {"inline": st.sampled_from(["1", "0"])}),
    ViewCase("ops_review_tickets", may("ticketing_admin", "gold_team"), query=_filters),
    ViewCase(
        "ops_batch_verify_tickets",
        may("ticketing_admin", "gold_team"),
        post=lambda w: {"ticket_ids": st.one_of(st.sampled_from([str(i) for i in w.ids("ticket_id")]), hostile_text())},
        idempotent=(Ticket, TicketHistory),
    ),
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
