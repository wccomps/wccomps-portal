"""The view property rows for the core views: home, sign-in, Discord linking, the school list and ops pages."""

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st
from registration.forms import ShownSchoolForm

from core.admin_views.competition import _COMPETITION_ACTION_HANDLERS
from core.forms import (
    ActionForm,
    AppSlugForm,
    BroadcastForm,
    CloseOutForm,
    LinkConfirmForm,
    ReadinessFixForm,
    ResetPasswordsForm,
    SchoolInfoEditForm,
    SetMaxMembersForm,
    SetScheduleForm,
    SetTimeForm,
    StartMessageForm,
    SyncRolesForm,
    TeamActionForm,
    TeamsBulkActionForm,
)
from core.models import CompetitionConfig
from core.tests.views_harness import (
    EVERYONE,
    OFF_SITE,
    SIGNED_IN,
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
from team.forms import CSVUploadForm, ImportConfirmForm, ImportEventForm
from team.models import SchoolInfo, Team
from ticketing.forms import TicketCategoryForm
from ticketing.models import TicketCategory

GOLD = may("gold_team")
OPS = may("admin", "gold_team")


def _fields(*built: Any, **hints: st.SearchStrategy[Any]) -> Fields:
    merged: Fields = {}
    for form in built:
        merged |= form_fields(form, hints)
    return merged


def _competition_action(w: World) -> Fields:
    return _fields(
        ActionForm(),
        SetMaxMembersForm(),
        StartMessageForm(),
        AppSlugForm(),
        SetTimeForm(),
        SetScheduleForm(),
        ResetPasswordsForm(),
        ReadinessFixForm(),
        CloseOutForm(),
        action=st.sampled_from(sorted(_COMPETITION_ACTION_HANDLERS)),
    )


def _school_import(w: World) -> Fields:
    return _fields(CSVUploadForm(), ImportEventForm(season=w.season), ImportConfirmForm()) | {
        "upload": st.just(""),
        "confirm": st.just(""),
        "csv_file": uploads(),
    }


CASES = [
    ViewCase("home", SIGNED_IN - {"no_role"}),
    ViewCase("health_check", EVERYONE, json=True),
    ViewCase("livez", EVERYONE),
    ViewCase("oauth_login", EVERYONE),
    ViewCase(
        "oauth_callback",
        EVERYONE,
        query=lambda w: {name: hostile_text() for name in ("error", "error_description", "state", "code")},
    ),
    ViewCase("oauth_logout", EVERYONE),
    ViewCase(
        "link_initiate",
        EVERYONE,
        query=lambda w: {"token": st.one_of(st.just("prop-link-token"), hostile_text())},
        sends_to_sign_in="a link token starts the sign-in that the link callback finishes",
    ),
    ViewCase(
        "link_callback",
        SIGNED_IN,
        query=lambda w: {"token": st.one_of(st.just("prop-link-token"), hostile_text())},
        post=lambda w: _fields(LinkConfirmForm(), token=st.just("prop-link-token")),
    ),
    ViewCase("school_info", GOLD),
    ViewCase("school_info_export", GOLD),
    ViewCase("school_info_clear", GOLD),
    ViewCase("school_info_import", GOLD, post=_school_import),
    ViewCase(
        "school_info_edit",
        GOLD,
        post=lambda w: _fields(SchoolInfoEditForm()),
        idempotent=(SchoolInfo, Team),
    ),
    ViewCase(
        "school_info_remove",
        GOLD,
        post=lambda w: _fields(ShownSchoolForm()),
    ),
    ViewCase("ops_ticket_list_redirect", SIGNED_IN),
    ViewCase("ops_ticket_detail_redirect", SIGNED_IN),
    ViewCase("admin_competition", OPS),
    ViewCase(
        "admin_competition_action",
        OPS,
        post=_competition_action,
        json=True,
        methods=("POST",),
        idempotent=(CompetitionConfig,),
    ),
    ViewCase("admin_competition_apps", OPS, json=True),
    ViewCase("admin_competition_guide", OPS),
    ViewCase("admin_competition_setup", OPS),
    ViewCase("admin_teams", OPS),
    ViewCase(
        "admin_teams_bulk_action",
        OPS,
        post=lambda w: _fields(TeamsBulkActionForm()),
        json=True,
        methods=("POST",),
        idempotent=(Team,),
    ),
    ViewCase("admin_team_detail", OPS),
    ViewCase(
        "admin_team_action",
        OPS,
        post=lambda w: _fields(TeamActionForm(), discord_id=st.sampled_from([str(7_000_000_002), "1"])),
        json=True,
        methods=("POST",),
    ),
    ViewCase("admin_broadcast", OPS),
    ViewCase("admin_broadcast_action", OPS, post=lambda w: _fields(BroadcastForm()), json=True, methods=("POST",)),
    ViewCase("admin_sync_roles", OPS),
    ViewCase("admin_sync_roles_action", OPS, post=lambda w: _fields(SyncRolesForm()), json=True, methods=("POST",)),
    ViewCase("admin_task_status", OPS, json=True),
    ViewCase("admin_categories", may("gold_team", "ticketing_admin")),
    ViewCase(
        "admin_category_create",
        may("gold_team", "ticketing_admin"),
        post=lambda w: _fields(TicketCategoryForm()),
    ),
    ViewCase(
        "admin_category_edit",
        may("gold_team", "ticketing_admin"),
        post=lambda w: _fields(TicketCategoryForm()),
        idempotent=(TicketCategory,),
    ),
    ViewCase("admin_category_delete", may("gold_team", "ticketing_admin")),
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


@given(next_url=st.one_of(st.sampled_from(OFF_SITE), hostile_text(), st.just("/tickets/?status=open")))
def test_sign_in_carries_only_a_same_site_next(view_world: World, next_url: str) -> None:
    """The next page a sign-in returns to rides in the signed state through Authentik: it is never off the site."""
    from django.core import signing
    from django.utils.http import url_has_allowed_host_and_scheme

    response = view_world.clients["anonymous"].get(reverse("oauth_login"), {"next": next_url})
    if "\x00" in next_url:
        assert response.status_code == 400  # refused before the view (core.middleware.RejectNulMiddleware)
        return
    assert response.status_code == 302
    state = parse_qs(urlsplit(response["Location"]).query)["state"][0]
    carried = signing.loads(state)["next"]
    assert url_has_allowed_host_and_scheme(carried[:2000], allowed_hosts={"testserver"}), carried
