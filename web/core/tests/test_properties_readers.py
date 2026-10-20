"""Property tests for every reader of JSON another service answers (Authentik, Quotient, the OAuth provider): given
any answer (any status, JSON of any shape, JSON too deep to read, or no JSON at all), the reader returns its
documented result or raises its documented error, and its callers answer or log instead of failing.

test_property_coverage finds the readers (a response's .json(), or json.loads of a response's text or content) and
fails on one without a case in READERS. A case serves each of the reader's requests an answer near the example JSON
that request gets (external_json), calls the reader, and checks what comes back.
"""

import contextlib
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from hypothesis import example, given
from hypothesis import strategies as st
from quotient.client import (
    Inject,
    QuotientBox,
    QuotientClient,
    QuotientInfrastructure,
    QuotientService,
    ServiceCheck,
    TeamServiceExport,
    TeamUptime,
)

from core.authentik_manager import AuthentikAPIError, AuthentikManager
from core.proxmox import ProxmoxAPIError, ProxmoxClient, ProxmoxSnapshot, ProxmoxTaskStatus, ProxmoxVM
from core.tests.external_json import Answer, answer_lists, deep, serving
from core.tests.strategies import rolled_back
from core.tests.views_harness import views_profile


@pytest.fixture(autouse=True)
def _silence_reader_logs() -> Iterator[None]:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


# ---------------------------------------------------------------------------------------------------------------
# What each request gets, in its real shape
# ---------------------------------------------------------------------------------------------------------------

PAGE = {"next": 0, "count": 1}
AUTHENTIK_USER = {
    "pk": 7,
    "username": "team01",
    "uid": "uid-7",
    "is_active": True,
    "groups": ["g1"],
    "groups_obj": [{"pk": "g1", "name": "WCComps_BlueTeam01"}],
    "attributes": {"discord_id": "1"},
}
USERS = {"results": [AUTHENTIK_USER], "pagination": PAGE}
GROUPS = {"results": [{"pk": "g1", "name": "WCComps_BlueTeam01", "parents": []}], "pagination": PAGE}
SESSIONS = {"results": [{"uuid": "s-1"}], "pagination": PAGE}
PROVIDERS = {"results": [{"pk": 1, "assigned_application_slug": "netbird"}], "pagination": PAGE}
APPLICATION = {"pk": "app-1", "slug": "netbird", "name": "Netbird"}
BINDINGS = {"results": [{"pk": "b-1", "enabled": True, "group": "g1", "group_obj": {"name": "WCComps_BlueTeam"}}]}
ERROR = {"detail": "Not allowed"}
DONE: dict[str, Any] = {}

METADATA = {"boxes": [{"name": "web", "ip": "10.0.0.5", "services": ["http", "ssh"]}]}
INJECTS = [{"ID": 1, "Title": "Inject 01 - Web (100 pts)", "Description": "Build a page"}]
EXPORT = [
    {
        "team_name": "team01",
        "services": [{"service_name": "web-http", "service_points": 100, "sla_violations": 1, "sla_penalty": 5}],
        "gross_points": 100,
        "total_sla_penalty": 5,
        "total_points": 95,
    }
]
UPTIMES = {"series": [{"Name": "team01", "Data": [{"Service": "web-http", "Uptime": 0.95}]}]}
SERVICE_CHECKS = [
    {
        "Round": {"ID": 12, "StartTime": "2026-10-18T19:30:00Z"},
        "Result": False,
        "Debug": "HTTP 502 from upstream",
        "Error": "unexpected status code",
    }
]
PROXMOX_POOL = {
    "data": {
        "members": [
            {"type": "qemu", "vmid": 10105, "name": "team01-web", "node": "pve01", "status": "running", "template": 0}
        ]
    }
}
PROXMOX_SNAPSHOTS = {
    "data": [
        {"name": "clean", "description": "Clean baseline", "snaptime": 1700000000},
        {"name": "current", "description": "You are here!"},
    ]
}
PROXMOX_UPID = {"data": "UPID:pve01:00001A2B:00000001:65000000:qmrollback:10105:user@pam!token:"}
PROXMOX_TASK_STATUS = {"data": {"status": "stopped", "exitstatus": "OK"}}


# ---------------------------------------------------------------------------------------------------------------
# What each reader documents
# ---------------------------------------------------------------------------------------------------------------


def is_str(value: object) -> bool:
    return isinstance(value, str)


def is_int(value: object) -> bool:
    return type(value) is int


def outcome(result: object, ok: Callable[[object], bool], what: str) -> None:
    assert ok(result), f"{what}, not {result!r:.300}"


def check_pair(result: object) -> None:
    outcome(
        result, lambda r: isinstance(r, tuple) and len(r) == 2 and type(r[0]) is bool and is_str(r[1]), "(bool, str)"
    )


def check_users(result: object) -> None:
    outcome(
        result,
        lambda r: (
            isinstance(r, list)
            and all(
                isinstance(u, dict)
                and is_str(u.get("uid"))
                and type(u.get("is_active")) is bool
                and _strs(u.get("groups"))
                for u in r
            )
        ),
        "users with a uid, is_active and group pks",
    )


def check_groups(result: object) -> None:
    outcome(
        result,
        lambda r: (
            isinstance(r, list)
            and all(
                isinstance(g, dict) and is_str(g.get("pk")) and is_str(g.get("name")) and _strs(g.get("parents"))
                for g in r
            )
        ),
        "groups with a pk, name and parent pks",
    )


def _strs(value: object) -> bool:
    return isinstance(value, list) and all(is_str(v) for v in value)


def check_user(result: object) -> None:
    outcome(
        result,
        lambda r: (
            r is None
            or (
                isinstance(r, dict)
                and is_int(r.get("pk"))
                and is_str(r.get("username", ""))
                and isinstance(r.get("groups_obj", []), list)
                and all(isinstance(g, dict) and is_str(g.get("name")) for g in r.get("groups_obj", []))
            )
        ),
        "None or a user with an int pk and named groups",
    )


def check_infrastructure(result: object) -> None:
    def ok(r: object) -> bool:
        return r is None or (
            isinstance(r, QuotientInfrastructure)
            and all(
                isinstance(box, QuotientBox)
                and is_str(box.name)
                and is_str(box.ip)
                and all(
                    isinstance(s, QuotientService) and is_str(s.name) and is_str(s.display_name) and is_str(s.type)
                    for s in box.services
                )
                for box in r.boxes
            )
        )

    outcome(result, ok, "None or boxes with names, IPs and named services")


def check_injects(result: object) -> None:
    outcome(
        result,
        lambda r: (
            r is None
            or (
                isinstance(r, list)
                and all(
                    isinstance(i, Inject) and is_int(i.inject_id) and is_str(i.title) and is_str(i.description)
                    for i in r
                )
            )
        ),
        "None or injects with an int id, a title and a description",
    )


def check_export(result: object) -> None:
    def ok(r: object) -> bool:
        return r is None or (
            isinstance(r, list)
            and all(
                isinstance(t, TeamServiceExport)
                and is_str(t.team_name)
                and all(is_int(n) for n in (t.team_number, t.gross_points, t.total_sla_penalty, t.total_points))
                and all(
                    is_str(s.service_name)
                    and all(is_int(n) for n in (s.service_points, s.sla_violations, s.sla_penalty))
                    for s in t.services
                )
                for t in r
            )
        )

    outcome(result, ok, "None or teams' services with whole numbers")


def check_uptimes(result: object) -> None:
    outcome(
        result,
        lambda r: (
            r is None
            or (
                isinstance(r, list)
                and all(
                    isinstance(t, TeamUptime)
                    and is_str(t.team_name)
                    and is_int(t.team_number)
                    and all(is_str(k) and type(v) is float and 0 <= v <= 1 for k, v in t.uptimes.items())
                    for t in r
                )
            )
        ),
        "None or teams' uptimes as fractions",
    )


def check_service_checks(result: object) -> None:
    outcome(
        result,
        lambda r: (
            r is None
            or (
                isinstance(r, list)
                and all(
                    isinstance(c, ServiceCheck)
                    and is_int(c.round_number)
                    and (c.timestamp is None or isinstance(c.timestamp, datetime))
                    and type(c.passed) is bool
                    and all(is_str(s) for s in (c.debug, c.error))
                    for c in r
                )
            )
        ),
        "None or service checks with a round, timestamp, pass/fail, debug and error",
    )


def check_proxmox_vms(result: object) -> None:
    outcome(
        result,
        lambda r: (
            r is None
            or (
                isinstance(r, list)
                and all(
                    isinstance(v, ProxmoxVM)
                    and is_int(v.vmid)
                    and 10100 <= v.vmid <= 10199
                    and all(is_str(s) and s for s in (v.name, v.host, v.node, v.status))
                    for v in r
                )
            )
        ),
        "None or team VMs with vmid, name, host, node and status",
    )


def check_proxmox_snapshots(result: object) -> None:
    outcome(
        result,
        lambda r: (
            r is None
            or (
                isinstance(r, list)
                and all(
                    isinstance(s, ProxmoxSnapshot) and is_str(s.name) and s.name != "current" and is_str(s.description)
                    for s in r
                )
            )
        ),
        "None or snapshots with a non-current name, and description",
    )


def check_proxmox_task(result: object) -> None:
    outcome(
        result,
        lambda r: isinstance(r, ProxmoxTaskStatus) and is_str(r.status) and is_str(r.exitstatus),
        "a ProxmoxTaskStatus with status and exitstatus",
    )


@dataclass(frozen=True)
class Reader:
    """One reader: how to call it, the JSON its requests get in order, its documented result and errors, and the
    functions the call reads through (as test_property_coverage names them: path:Class.function)."""

    name: str
    call: Callable[[], object]
    examples: tuple[Any, ...]
    check: Callable[[object], None]
    keys: tuple[str, ...]
    raises: tuple[type[BaseException], ...] = ()

    def __repr__(self) -> str:
        return self.name


def authentik() -> AuthentikManager:
    return AuthentikManager(base_url="https://auth.test", api_token="token")


def quotient() -> QuotientClient:
    cache.clear()
    return QuotientClient(base_url="https://quotient.test")


def proxmox() -> ProxmoxClient:
    return ProxmoxClient(base_url="https://proxmox.test", api_token="user@pam!token=secret")


def error_from_answer() -> object:
    """_handle_response_error reads an error answer's body: it gets the first answer served, as is."""
    import httpx

    response = httpx.Client().get("https://auth.test/api/v3/core/users/")
    return authentik()._handle_response_error(response, "Read")


def check_bool(result: object) -> None:
    outcome(result, lambda r: type(r) is bool, "a bool")


def check_toggle(result: object) -> None:
    outcome(
        result,
        lambda r: isinstance(r, tuple) and ((r[0] is True and r[1] is None) or (r[0] is False and is_str(r[1]))),
        "(True, None) or (False, why)",
    )


def check_choices(result: object) -> None:
    outcome(
        result,
        lambda r: isinstance(r, list) and all(isinstance(c, dict) and all(is_str(v) for v in c.values()) for c in r),
        "choices of text",
    )


AM = "web/core/authentik_manager.py:AuthentikManager"
QC = "web/quotient/client.py:QuotientClient"
QJ = "web/quotient/client.py:_json"
PX = "web/core/proxmox.py:ProxmoxClient"
PJ = "web/core/proxmox.py:_json"
READ = (f"{AM}._results", f"{AM}._json")
USER = (f"{AM}._first_user", *READ)
READERS = [
    Reader(
        "_handle_response_error",
        error_from_answer,
        (ERROR,),
        lambda r: outcome(r, lambda e: isinstance(e, AuthentikAPIError), "an AuthentikAPIError"),
        (f"{AM}._handle_response_error",),
    ),
    Reader(
        "list_applications",
        lambda: authentik().list_applications(),
        (PROVIDERS,),
        lambda r: outcome(r, _strs, "slugs"),
        (f"{AM}.list_applications", *READ),
    ),
    Reader(
        "get_application_by_slug",
        lambda: authentik().get_application_by_slug("netbird"),
        (APPLICATION,),
        lambda r: outcome(r, lambda a: a is None or (isinstance(a, dict) and is_str(a.get("pk"))), "None or an app"),
        (f"{AM}.get_application_by_slug",),
    ),
    Reader(
        "get_blueteam_binding",
        lambda: authentik().get_blueteam_binding("app-1"),
        (BINDINGS,),
        lambda r: outcome(
            r,
            lambda b: (
                isinstance(b, tuple)
                and (
                    (isinstance(b[0], dict) and is_str(b[0].get("pk")) and b[1] is None)
                    or (b[0] is None and is_str(b[1]))
                )
            ),
            "(binding, None) or (None, why)",
        ),
        (f"{AM}.get_blueteam_binding", *READ),
    ),
    Reader(
        "update_binding_enabled",
        lambda: authentik().update_binding_enabled({"pk": "b-1", "enabled": True}, enabled=False),
        (DONE,),
        check_bool,
        (f"{AM}.update_binding_enabled",),
    ),
    Reader(
        "enable_application",
        lambda: authentik().enable_application("netbird"),
        (APPLICATION, BINDINGS, DONE),
        check_toggle,
        (f"{AM}.enable_application", f"{AM}._toggle_application"),
    ),
    Reader(
        "disable_application",
        lambda: authentik().disable_application("netbird"),
        (APPLICATION, BINDINGS, DONE),
        check_toggle,
        (f"{AM}.disable_application", f"{AM}._toggle_application"),
    ),
    Reader(
        "update_user_discord_id",
        lambda: authentik().update_user_discord_id("team01", 1, "uid-7"),
        (USERS, DONE),
        check_bool,
        (f"{AM}.update_user_discord_id", *USER),
        raises=(httpx.HTTPStatusError,),
    ),
    Reader(
        "revoke_user_sessions",
        lambda: authentik().revoke_user_sessions("team01"),
        (USERS, SESSIONS, DONE),
        lambda r: outcome(
            r,
            lambda t: (
                isinstance(t, tuple)
                and type(t[0]) is bool
                and (t[1] is None or is_str(t[1]))
                and is_int(t[2])
                and t[2] >= 0
            ),
            "(ok, error, revoked)",
        ),
        (f"{AM}.revoke_user_sessions", *USER),
    ),
    Reader(
        "toggle_user",
        lambda: authentik().toggle_user("team01", True),
        (USERS, DONE),
        check_pair,
        (f"{AM}.toggle_user", *USER),
    ),
    Reader(
        "reset_blueteam_password",
        lambda: authentik().reset_blueteam_password(1, "Correct-Horse-1!"),
        (USERS, DONE),
        check_pair,
        (f"{AM}.reset_blueteam_password", *USER),
    ),
    Reader(
        "list_all_users",
        lambda: authentik().list_all_users(),
        (USERS,),
        check_users,
        (f"{AM}.list_all_users", f"{AM}._list_all", *READ),
        raises=(AuthentikAPIError,),
    ),
    Reader(
        "list_all_groups",
        lambda: authentik().list_all_groups(),
        (GROUPS,),
        check_groups,
        (f"{AM}.list_all_groups", f"{AM}._list_all", *READ),
        raises=(AuthentikAPIError,),
    ),
    Reader(
        "get_user_with_groups",
        lambda: authentik().get_user_with_groups("team01"),
        (USERS,),
        check_user,
        (f"{AM}.get_user_with_groups", *USER),
    ),
    Reader(
        "get_group_by_name",
        lambda: authentik().get_group_by_name("WCComps_BlueTeam01"),
        (GROUPS,),
        lambda r: outcome(
            r,
            lambda g: (
                g is None or (isinstance(g, dict) and g.get("name") == "WCComps_BlueTeam01" and is_str(g.get("pk")))
            ),
            "None or the group",
        ),
        (f"{AM}.get_group_by_name", *READ),
    ),
    Reader(
        "add_user_to_group",
        lambda: authentik().add_user_to_group(7, "g1"),
        (DONE,),
        check_pair,
        (f"{AM}.add_user_to_group",),
    ),
    Reader(
        "get_infrastructure",
        lambda: quotient().get_infrastructure(force_refresh=True),
        (DONE, METADATA),
        check_infrastructure,
        (f"{QC}.get_infrastructure", QJ),
    ),
    Reader(
        "get_service_choices",
        lambda: quotient().get_service_choices(),
        (DONE, METADATA),
        check_choices,
        (f"{QC}.get_service_choices",),
    ),
    Reader(
        "get_box_names",
        lambda: quotient().get_box_names(),
        (DONE, METADATA),
        lambda r: outcome(r, _strs, "box names"),
        (f"{QC}.get_box_names",),
    ),
    Reader(
        "get_injects",
        lambda: quotient().get_injects(force_refresh=True),
        (DONE, INJECTS),
        check_injects,
        (f"{QC}.get_injects", QJ),
    ),
    Reader(
        "get_service_export",
        lambda: quotient().get_service_export(force_refresh=True),
        (DONE, EXPORT),
        check_export,
        (f"{QC}.get_service_export", QJ),
    ),
    Reader(
        "get_uptimes",
        lambda: quotient().get_uptimes(force_refresh=True),
        (DONE, UPTIMES),
        check_uptimes,
        (f"{QC}.get_uptimes", QJ),
    ),
    Reader(
        "get_service_checks",
        lambda: quotient().get_service_checks(1, "web:http", force_refresh=True),
        (DONE, SERVICE_CHECKS),
        check_service_checks,
        (f"{QC}.get_service_checks", QJ),
    ),
    Reader(
        "proxmox_list_team_vms",
        lambda: proxmox().list_team_vms(1),
        (PROXMOX_POOL,),
        check_proxmox_vms,
        (f"{PX}.list_team_vms", PJ),
    ),
    Reader(
        "proxmox_list_snapshots",
        lambda: proxmox().list_snapshots("pve01", 10105),
        (PROXMOX_SNAPSHOTS,),
        check_proxmox_snapshots,
        (f"{PX}.list_snapshots", PJ),
    ),
    Reader(
        "proxmox_rollback_snapshot",
        lambda: proxmox().rollback_snapshot("pve01", 10105, "clean"),
        (PROXMOX_UPID,),
        lambda r: outcome(r, lambda u: is_str(u) and bool(u), "a non-empty task UPID"),
        (f"{PX}.rollback_snapshot", PJ),
        raises=(ProxmoxAPIError,),
    ),
    Reader(
        "proxmox_start_vm",
        lambda: proxmox().start_vm("pve01", 10105),
        (PROXMOX_UPID,),
        lambda r: outcome(r, lambda u: is_str(u) and bool(u), "a non-empty task UPID"),
        (f"{PX}.start_vm", PJ),
        raises=(ProxmoxAPIError,),
    ),
    Reader(
        "proxmox_wait_for_task",
        lambda: proxmox().wait_for_task("pve01", "UPID:pve01:1", timeout_seconds=0.0, poll_interval=0.0),
        (PROXMOX_TASK_STATUS,),
        check_proxmox_task,
        (f"{PX}.wait_for_task", f"{PX}.get_task_status", PJ),
        raises=(ProxmoxAPIError,),
    ),
]


@pytest.mark.parametrize("reader", READERS, ids=repr)
@given(data=st.data())
@example(data=None)
def test_any_answer_gets_the_documented_result(reader: Reader, data: st.DataObject | None) -> None:
    """The reader returns its documented result, or raises its documented error, for any answers. The example: JSON
    nested past Python's recursion limit, where json.loads raises RecursionError (no ValueError)."""
    answers = (
        data.draw(answer_lists(*reader.examples), label="answers") if data is not None else [Answer(200, deep(100_000))]
    )
    with override_settings(QUOTIENT_USERNAME="property", QUOTIENT_PASSWORD="property"), serving(answers):
        try:
            result = reader.call()
        except reader.raises:
            return
    reader.check(result)


# ---------------------------------------------------------------------------------------------------------------
# The callers: they answer or log, whatever the other service says
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """A function that reads another service through a reader, the JSON its requests get, and its documented
    errors."""

    name: str
    call: Callable[[], object]
    examples: tuple[Any, ...]
    raises: tuple[type[BaseException], ...] = field(default=())

    def __repr__(self) -> str:
        return self.name


def _sync_metadata() -> object:
    from scoring.quotient_sync import sync_quotient_metadata

    return sync_quotient_metadata()


def _sync_scores() -> object:
    from scoring.quotient_sync import sync_service_scores

    return sync_service_scores()


def _refresh_groups() -> object:
    from core.services.user_groups import refresh_user_groups

    return refresh_user_groups(authentik())


def _reset_credentials() -> object:
    from core.authentik_utils import reset_team_credentials

    return reset_team_credentials(1)


def _store_discord_id() -> object:
    from core.services.linking import store_discord_id_in_authentik

    return store_discord_id_in_authentik("team01", 1, "uid-7")


def _start_competition() -> object:
    from core.models import CompetitionConfig
    from core.services.competition import run_competition_to_completion

    CompetitionConfig.objects.filter(pk=CompetitionConfig.get_config().pk).update(controlled_applications=["netbird"])
    return run_competition_to_completion(True, "property")


def _readiness() -> object:
    from core.admin_views.readiness import stream_readiness_checks

    return [json.loads(line) for line in stream_readiness_checks()]


def _fix_groups() -> object:
    from django.test import RequestFactory

    from core.admin_views.readiness import _fix_group_membership

    return _fix_group_membership(RequestFactory().post("/"))


CALLERS = [
    Caller("sync_quotient_metadata", _sync_metadata, (DONE, METADATA, INJECTS), (ValueError,)),
    Caller("sync_service_scores", _sync_scores, (DONE, EXPORT, UPTIMES), (ValueError,)),
    Caller("refresh_user_groups", _refresh_groups, (GROUPS, USERS), (Exception,)),
    Caller("reset_team_credentials", _reset_credentials, (USERS, DONE, USERS, SESSIONS, DONE)),
    Caller("store_discord_id_in_authentik", _store_discord_id, (USERS, DONE)),
    Caller("run_competition_to_completion", _start_competition, (APPLICATION, BINDINGS, DONE, USERS, DONE, GROUPS)),
    Caller("stream_readiness_checks", _readiness, (USERS, USERS, APPLICATION, BINDINGS, DONE, METADATA)),
    Caller("_fix_group_membership", _fix_groups, (USERS, GROUPS, DONE)),
]
SERVICES = {
    "AUTHENTIK_URL": "https://auth.test",
    "AUTHENTIK_TOKEN": "token",
    "QUOTIENT_API_URL": "https://quotient.test",
    "QUOTIENT_USERNAME": "property",
    "QUOTIENT_PASSWORD": "property",
}


HUGE_EXPORT = [
    {**EXPORT[0], "gross_points": 10**30, "services": [{**EXPORT[0]["services"][0], "service_points": 10**30}]}
]


@pytest.mark.django_db
@pytest.mark.parametrize("caller", CALLERS, ids=repr)
@views_profile()
@given(data=st.data())
@example(
    data=[Answer(200, b"{}"), Answer(200, json.dumps(HUGE_EXPORT).encode()), Answer(200, json.dumps(UPTIMES).encode())]
)
def test_callers_answer_or_log_whatever_the_service_says(caller: Caller, data: st.DataObject | list[Answer]) -> None:
    """The caller returns, or raises only its documented error (refresh_user_groups: anything, as its callers catch
    every error), and leaves the database usable: a database error it swallowed would break the transaction."""
    from team.models import Team

    if not Team.objects.filter(team_number=1).exists():
        Team.objects.create(team_number=1, team_name="Team 01", authentik_group="WCComps_BlueTeam01")
    answers = data if isinstance(data, list) else data.draw(answer_lists(*caller.examples), label="answers")
    with rolled_back(), override_settings(**SERVICES), serving(answers):
        cache.clear()
        with contextlib.suppress(caller.raises):
            caller.call()
        assert Team.objects.filter(team_number=1).exists()


# ---------------------------------------------------------------------------------------------------------------
# The OAuth callback: the token and userinfo answers
# ---------------------------------------------------------------------------------------------------------------

OAUTH_CONFIG = {
    "client_id": "property-client",
    "client_secret": "property-secret",
    "authorization_endpoint": "https://auth.test/authorize/",
    "token_endpoint": "https://auth.test/token/",
    "userinfo_endpoint": "https://auth.test/userinfo/",
    "end_session_endpoint": "https://auth.test/end-session/",
}
TOKENS = {"access_token": "access", "token_type": "Bearer", "expires_in": 300}
USERINFO = {
    "sub": "sub-1",
    "preferred_username": "player",
    "email": "player@example.edu",
    "groups": ["WCComps_BlueTeam01"],
}


def signed_in_after(answers: list[Answer]) -> tuple[int, Client]:
    """The callback's status after a real login start, with the token and userinfo requests getting these answers."""
    client = Client()
    with patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG):
        state = parse_qs(urlparse(client.get("/auth/login/")["Location"]).query)["state"][0]
        with serving(answers):
            response = client.get("/auth/callback/", {"code": "code", "state": state})
    return response.status_code, client


@pytest.mark.django_db
@views_profile()
@given(answers=answer_lists(TOKENS, USERINFO))
@example(answers=[Answer(200, b"<html>502 Bad Gateway</html>")])
@example(
    answers=[Answer(200, json.dumps(TOKENS).encode()), Answer(200, json.dumps({**USERINFO, "groups": "x"}).encode())]
)
def test_any_token_or_userinfo_answer_signs_in_or_refuses(answers: list[Answer]) -> None:
    """The callback answers every token and userinfo answer without a 500, and signs in only a user with a text
    sub and username and a list of group names."""
    from core.models import UserGroups

    with rolled_back():
        status, _ = signed_in_after(answers)
        assert status < 500
        for row in UserGroups.objects.select_related("user"):
            assert isinstance(row.authentik_id, str) and row.authentik_id
            assert isinstance(row.groups, list) and all(isinstance(g, str) for g in row.groups)


# Readers inside a view, each covered by its own property above (READERS calls a reader directly)
VIEW_READERS = dict.fromkeys(
    ("web/core/oauth.py:oauth_callback", "web/core/oauth.py:_json_object"),
    test_any_token_or_userinfo_answer_signs_in_or_refuses,
)
