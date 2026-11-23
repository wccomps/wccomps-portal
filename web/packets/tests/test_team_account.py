"""Packet account activation, with a real database and a synthetic Authentik HTTP boundary."""

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from threading import Event as Signal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.apps import apps
from django.db import connection, connections
from registration.models import Event, School, Season
from scoring.models import ServiceScore

from core.authentik_manager import AuthentikManager
from core.models import AuditLog, CompetitionConfig
from core.services.competition import RunOutcome, run_competition
from core.team_accounts import TeamAccounts
from core.utils import drain
from packets.models import Packet, PacketDistribution
from packets.services import PacketDistributionService
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db

DISABLED = {"pk": 5, "username": "team01", "uid": "sub-01", "is_active": False, "groups": ["blue"], "type": "internal"}
STAFF_BINDING = {
    "pk": "staff-binding",
    "target": "app-1",
    "enabled": True,
    "group": "staff",
    "group_obj": {"name": "Staff"},
    "policy": None,
    "user": None,
    "negate": False,
    "dry_run": False,
    "expiring": False,
}
BLUE_BINDING = {
    **STAFF_BINDING,
    "pk": "blue-binding",
    "enabled": False,
    "group": "blue",
    "group_obj": {"name": "BlueTeam"},
}


@pytest.fixture
def distribution(current_event):
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"controlled_applications": ["vpn"]})
    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    SchoolInfo.objects.create(
        team=team, school=School.for_name("School One"), contact_email="c@example.edu", password="Stored1!"
    )
    packet = Packet.objects.create(
        title="Packet",
        file_data=b"pdf",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=3,
        uploaded_by="gold",
        status="distributing",
        send_via_email=True,
        event=current_event,
    )
    return PacketDistribution.objects.create(packet=packet, team=team)


def _page(results):
    return {"results": results, "pagination": {"next": 0}}


class FakeAuthentik:
    def __init__(self):
        self.user = dict(DISABLED)
        self.groups = [
            {"pk": "blue", "name": "BlueTeam", "parents": []},
            {"pk": "staff", "name": "Staff", "parents": []},
        ]
        self.app = {"pk": "app-1", "slug": "vpn", "policy_engine_mode": "any"}
        self.bindings = _page([BLUE_BINDING, STAFF_BINDING])
        self.next_bindings = _page([])
        self.permissions = _page(
            [
                {
                    "role_pk": "portal-role",
                    "model_permissions": [{"app_label": "authentik_policies", "codename": "view_policybinding"}],
                }
            ]
        )
        self.requests = []
        self.writes = []
        self.before_patch = lambda: None
        self.patch_status = 200

    def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method == "PATCH" and path == "/api/v3/core/users/5/":
            self.before_patch()
            self.writes.append((path, json.loads(request.content)))
            return httpx.Response(self.patch_status, json={})
        if request.method != "GET":
            raise AssertionError(f"Unexpected write: {request.method} {path}")
        if path == "/api/v3/core/users/me/":
            return httpx.Response(200, json={"user": {"is_superuser": False, "roles": [{"pk": "portal-role"}]}})
        if path == "/api/v3/rbac/permissions/assigned_by_roles/":
            assert request.url.params["model"] == "authentik_policies.policybinding"
            return httpx.Response(200, json=self.permissions)
        if path == "/api/v3/policies/bindings/" and request.url.params["page"] == "2":
            return httpx.Response(200, json=self.next_bindings)
        data = {
            "/api/v3/core/users/": _page([self.user]),
            "/api/v3/core/groups/": _page(self.groups),
            "/api/v3/core/applications/vpn/": self.app,
            "/api/v3/policies/bindings/": self.bindings,
        }
        return httpx.Response(200, json=data[path]) if path in data else httpx.Response(404)

    def manager(self):
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return AuthentikManager(base_url="https://auth.test", client=client)


@pytest.fixture
def authentik():
    fake = FakeAuthentik()
    with (
        patch("core.authentik_utils.AuthentikManager", side_effect=fake.manager),
        patch("core.admin_views.readiness.AuthentikManager", side_effect=fake.manager),
    ):
        yield fake


@pytest.fixture
def email():
    with patch("packets.services.EmailMultiAlternatives") as mock:
        yield mock.return_value.send


@pytest.fixture
def competition_run():
    with (
        patch("core.authentik_manager.AuthentikManager"),
        patch(
            "core.services.competition.accounts_by_team",
            side_effect=lambda _, ns: {n: TeamAccounts([], []) for n in ns},
        ),
        patch("core.services.user_groups.refresh_user_groups"),
        patch("scoring.quotient_sync.sync_service_scores", return_value={}),
    ):
        yield lambda enable: drain(run_competition(enable, "test-operator"))


def _send(distribution):
    PacketDistributionService().send_packet_email(distribution)


def test_a_packet_send_enables_the_account_without_changing_app_bindings(distribution, authentik, email):
    _send(distribution)

    assert authentik.writes == [("/api/v3/core/users/5/", {"is_active": True})]
    email.assert_called_once()
    row = AuditLog.objects.get(action="team_account_enabled")
    assert (row.admin_user, row.target_id, row.details) == ("packet distribution", 1, {"enabled": True, "error": None})


def test_an_already_active_account_is_left_alone(distribution, authentik, email):
    authentik.user["is_active"] = True
    _send(distribution)

    assert authentik.requests == [("GET", "/api/v3/core/users/")]
    assert not AuditLog.objects.filter(action="team_account_enabled").exists()


@pytest.mark.parametrize(
    "reason", ["running", "scores", "inactive team", "no event", "earlier event", "several accounts"]
)
def test_ineligible_packets_send_without_attempting_activation(distribution, authentik, email, settings, reason):
    if reason == "running":
        CompetitionConfig.objects.update(applications_enabled=True)
    elif reason == "scores":
        ServiceScore.objects.create(team=distribution.team, service_points=1)
    elif reason == "inactive team":
        Team.objects.filter(pk=distribution.team_id).update(is_active=False)
    elif reason == "no event":
        distribution.packet.event = None
        distribution.packet.save(update_fields=["event"])
    elif reason == "earlier event":
        old = distribution.packet.event
        Event.objects.create(season=old.season, name="Next", event_type="qualifier", date=date(2027, 1, 1)).open()
    elif reason == "several accounts":
        settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    _send(distribution)

    assert authentik.requests == []
    email.assert_called_once()


@pytest.mark.parametrize("start_first", [True, False], ids=["scoreless-start-stop", "stop-before-start"])
def test_stop_bars_resends_independently_of_schedule_or_scores(
    distribution, authentik, email, competition_run, start_first
):
    if start_first:
        competition_run(True)
        assert CompetitionConfig.get_config().started_this_window
    competition_run(False)
    assert not CompetitionConfig.get_config().started_this_window
    assert not ServiceScore.objects.exists()

    _send(distribution)

    assert authentik.requests == []
    email.assert_called_once()


def test_a_start_that_aborts_after_its_first_app_write_still_bars_resends(
    distribution, authentik, email, competition_run
):
    with closing(run_competition(True, "test-operator")) as steps:
        assert next(steps).message == "Enabled vpn"
    assert not CompetitionConfig.get_config().applications_enabled

    _send(distribution)

    assert authentik.requests == []
    email.assert_called_once()


def test_a_reopened_event_does_not_enable_an_account_from_an_old_packet(distribution, authentik, email):
    event = distribution.packet.event
    event.close()
    event.open()

    _send(distribution)

    assert authentik.requests == []
    email.assert_called_once()


def test_migration_refuses_account_activation_for_an_existing_event_with_unknown_history(
    distribution, authentik, email
):
    migrate = importlib.import_module("registration.migrations.0017_event_packet_activation_blocked")
    migrate.block_existing_events(apps, SimpleNamespace(connection=connection))

    _send(distribution)

    assert authentik.requests == []
    email.assert_called_once()
    new_event = Event.objects.create(
        season=Season.objects.active(), name="New", event_type="qualifier", date=date(2027, 1, 1)
    )
    assert not new_event.packet_activation_blocked


@pytest.mark.parametrize(
    "problem",
    [
        "no apps",
        "missing app",
        "unknown app mode",
        "missing binding",
        "duplicate blue",
        "blue enabled",
        "only blue enabled",
        "access through staff group",
        "access through inherited staff group",
        "unreadable ancestry",
        "unreadable account type",
        "negated staff",
        "dry run staff",
        "expiring staff",
        "unknown policy",
        "malformed enabled flag",
        "missing pagination",
        "object grant only",
        "unrelated global grant",
    ],
)
def test_an_unproved_denial_skips_activation_and_still_sends(distribution, authentik, email, problem):
    if problem == "no apps":
        CompetitionConfig.objects.update(controlled_applications=[])
    elif problem == "missing app":
        CompetitionConfig.objects.update(controlled_applications=["vpn", "second"])
    elif problem == "unknown app mode":
        authentik.app["policy_engine_mode"] = None
    elif problem == "missing binding":
        authentik.bindings = _page([STAFF_BINDING])
    elif problem == "duplicate blue":
        authentik.bindings = _page([BLUE_BINDING, {**BLUE_BINDING, "pk": "other"}, STAFF_BINDING])
    elif problem == "blue enabled":
        authentik.bindings = _page([{**BLUE_BINDING, "enabled": True}, STAFF_BINDING])
    elif problem == "only blue enabled":
        authentik.bindings = _page([BLUE_BINDING, {**STAFF_BINDING, "enabled": False}])
    elif problem == "access through staff group":
        authentik.user["groups"] = ["blue", "staff"]
    elif problem == "access through inherited staff group":
        authentik.groups[0]["parents"] = ["staff"]
    elif problem == "unreadable ancestry":
        authentik.groups[0]["parents"] = ["hidden"]
    elif problem == "unreadable account type":
        authentik.user["type"] = "service_account"
    elif problem == "negated staff":
        authentik.bindings = _page([BLUE_BINDING, {**STAFF_BINDING, "negate": True}])
    elif problem == "dry run staff":
        authentik.bindings = _page([BLUE_BINDING, {**STAFF_BINDING, "dry_run": True}])
    elif problem == "expiring staff":
        authentik.bindings = _page([BLUE_BINDING, {**STAFF_BINDING, "expiring": True}])
    elif problem == "unknown policy":
        authentik.bindings = _page([BLUE_BINDING, STAFF_BINDING, {**STAFF_BINDING, "pk": "policy", "policy": "p1"}])
    elif problem == "malformed enabled flag":
        authentik.bindings = _page(
            [BLUE_BINDING, STAFF_BINDING, {**STAFF_BINDING, "pk": "unknown", "enabled": "false"}]
        )
    elif problem == "missing pagination":
        authentik.bindings.pop("pagination")
    elif problem == "object grant only":
        role = authentik.permissions["results"][0]
        role["object_permissions"] = role.pop("model_permissions")
    elif problem == "unrelated global grant":
        authentik.permissions["results"][0]["role_pk"] = "unrelated-role"

    _send(distribution)

    assert authentik.writes == []
    row = AuditLog.objects.get(action="team_account_enabled")
    assert row.details["enabled"] is False and row.details["error"]
    email.assert_called_once()


def test_an_enabled_binding_on_the_next_page_can_block_activation(distribution, authentik, email):
    authentik.bindings["pagination"]["next"] = 2
    authentik.next_bindings = _page([{**STAFF_BINDING, "pk": "more", "group": "blue"}])

    _send(distribution)

    assert authentik.writes == []
    assert "belongs to an enabled access group" in AuditLog.objects.get(action="team_account_enabled").details["error"]
    email.assert_called_once()


def test_readiness_uses_the_same_membership_check(distribution, authentik):
    from core.admin_views.readiness import _check_blueteam_bindings

    assert _check_blueteam_bindings()[0] == "pass"
    authentik.groups[0]["parents"] = ["staff"]
    severity, detail, _ = _check_blueteam_bindings()
    assert severity == "fail" and "belongs to an enabled access group" in detail


def test_a_failure_to_turn_it_on_is_audited_and_the_email_still_goes(distribution, authentik, email):
    authentik.patch_status = 500
    _send(distribution)

    email.assert_called_once()
    row = AuditLog.objects.get(action="team_account_enabled")
    assert row.details == {"enabled": False, "error": "Set team01 active=True: Authentik server error"}


@pytest.mark.django_db(transaction=True)
def test_stop_cannot_pass_in_flight_activation_but_can_run_during_smtp(distribution, authentik, email, competition_run):
    patch_started, allow_patch = Signal(), Signal()
    smtp_started, allow_smtp = Signal(), Signal()

    def block_patch():
        patch_started.set()
        assert allow_patch.wait(10)

    def block_smtp(*args, **kwargs):
        smtp_started.set()
        assert allow_smtp.wait(10)

    def worker():
        try:
            _send(distribution)
        finally:
            connections.close_all()

    authentik.before_patch = block_patch
    email.side_effect = block_smtp
    with ThreadPoolExecutor(max_workers=1) as pool:
        sent = pool.submit(worker)
        try:
            assert patch_started.wait(10)
            assert competition_run(False).outcome is RunOutcome.REFUSED
            allow_patch.set()
            assert smtp_started.wait(10)
            assert competition_run(False).outcome is RunOutcome.COMPLETE
        finally:
            allow_patch.set()
            allow_smtp.set()
        sent.result(timeout=10)
    assert Event.objects.get(is_active=True).packet_activation_blocked
