"""The view property rows for the Django admin: the site's own pages and one row per registered model (built from
the registry, so a new model is driven too)."""

from typing import Any

import pytest
from django.contrib import admin
from django.db import models
from django.test import RequestFactory
from hypothesis import strategies as st

from core.tests.views_harness import (
    Fields,
    ViewCase,
    World,
    any_request_is_answered_safely,
    echo_sweep,
    every_allowed_role_gets_in,
    form_fields,
    hostile_text,
    may,
)

ADMINS = may("admin")  # the admin site lets in only the admin permission


def _list_filters(model_admin: admin.ModelAdmin[Any]) -> list[str]:
    names = []
    for entry in model_admin.list_filter:
        if isinstance(entry, str):
            names += [entry, f"{entry}__exact", f"{entry}__id__exact", f"{entry}__isnull"]
        elif isinstance(entry, (list, tuple)) and isinstance(entry[0], str):
            names.append(entry[0])
        elif hasattr(entry, "parameter_name"):
            names.append(entry.parameter_name)
    return names


def _changelist_query(model_admin: admin.ModelAdmin[Any]) -> Any:
    def query(w: World) -> Fields:
        fields: Fields = {name: hostile_text() for name in ("q", "o", "p", "all", "_popup", "e", "_to_field")}
        fields["o"] = st.one_of(st.lists(st.integers(-12, 12), max_size=3).map(lambda o: ".".join(map(str, o))),
                                hostile_text())  # fmt: skip
        fields["p"] = st.one_of(st.integers(-1, 3).map(str), hostile_text())
        return fields | {name: hostile_text() for name in _list_filters(model_admin)}

    return query


def _admin_post(model: type[models.Model], model_admin: admin.ModelAdmin[Any]) -> Any:
    def post(w: World) -> Fields:
        request = RequestFactory().get("/")
        request.user = w.users["admin"]
        fields: Fields = {}
        if model_admin.has_add_permission(request):
            fields |= form_fields(model_admin.get_form(request)())
        pks = [str(pk) for pk in w.ids(f"pk:{model._meta.label}")]
        selected = st.one_of(st.lists(st.sampled_from(pks), min_size=1), hostile_text()) if pks else hostile_text()
        actions = sorted(model_admin.get_actions(request))
        return fields | {
            "action": st.one_of(st.sampled_from(actions), hostile_text()) if actions else hostile_text(),
            "_selected_action": selected,
            "index": st.sampled_from(["0", "1", "-1"]),
            "select_across": st.sampled_from(["0", "1"]),
            "post": st.just("yes"),
            "_save": st.just("Save"),
        }

    return post


def _model_case(model: type[models.Model], model_admin: admin.ModelAdmin[Any]) -> ViewCase:
    prefix = f"admin:{model._meta.app_label}_{model._meta.model_name}"
    return ViewCase(
        f"{prefix}_changelist",
        ADMINS,
        also=tuple(f"{prefix}_{page}" for page in ("add", "change", "delete", "history")),
        covers=f"admin:{model._meta.label}",
        params={"object_id": f"pk:{model._meta.label}"},
        query=_changelist_query(model_admin),
        post=_admin_post(model, model_admin),
        gate_names=(f"{prefix}_changelist",),
    )


def _site_query(w: World) -> Fields:
    return {
        "app_label": st.one_of(st.sampled_from(["team", "ticketing", "scoring"]), hostile_text()),
        "model_name": st.one_of(st.sampled_from(["team", "ticket", "redteamscore"]), hostile_text()),
        "field_name": st.one_of(st.sampled_from(["team", "category", "assigned_to"]), hostile_text()),
        "term": hostile_text(),
        "page": hostile_text(),
    }


SITE = ViewCase(
    "admin:index",
    ADMINS,
    also=(
        "admin:login",
        "admin:logout",
        "admin:password_change",
        "admin:password_change_done",
        "admin:jsi18n",
        "admin:app_list",
    ),
    covers="url:admin/",
    params={"app_label": "app_label"},
    query=_site_query,
    post=lambda w: {name: hostile_text() for name in ("old_password", "new_password1", "new_password2")},
)
# No model has a page on the site (get_absolute_url), so Django's shortcut finds none for anyone
VIEW_ON_SITE = ViewCase(
    "admin:view_on_site",
    frozenset(),
    covers="url:admin/",
    params={"content_type_id": "pk:contenttypes.ContentType", "object_id": "team_id"},
)
AUTOCOMPLETE = ViewCase(
    "admin:autocomplete",
    ADMINS,
    covers="url:admin/",
    query=_site_query,
    # The one field an admin form searches (scoring's RedTeamScoreAdmin autocomplete_fields)
    entry_query=lambda w: {"app_label": ["scoring"], "model_name": ["redteamscore"], "field_name": ["attack_type"]},
)
CASES = [
    SITE,
    VIEW_ON_SITE,
    AUTOCOMPLETE,
    *(_model_case(model, model_admin) for model, model_admin in admin.site._registry.items()),
]


@pytest.mark.parametrize("case", CASES, ids=[c.entry_point for c in CASES])
def test_any_request_is_answered_safely(case: ViewCase, view_world: World) -> None:
    any_request_is_answered_safely(case, view_world)


@pytest.mark.parametrize("case", CASES, ids=[c.entry_point for c in CASES])
def test_every_field_is_escaped_where_echoed(case: ViewCase, view_world: World) -> None:
    echo_sweep(case, view_world)


@pytest.mark.parametrize("case", CASES, ids=[c.entry_point for c in CASES])
def test_every_allowed_role_gets_in(case: ViewCase, view_world: World) -> None:
    every_allowed_role_gets_in(case, view_world)
