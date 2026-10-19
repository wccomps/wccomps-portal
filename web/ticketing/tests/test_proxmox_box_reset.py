"""Tests for Proxmox VE API client, RangeKiln VM classification, and ticket box resets."""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse

from core.models import AuditLog, DiscordTask
from core.proxmox import (
    ProxmoxAPIError,
    ProxmoxClient,
    ProxmoxSnapshot,
    ProxmoxTaskStatus,
    ProxmoxVM,
    classify_team_vm,
    is_valid_snapshot_name,
    match_team_vm,
    normalize_ticket_host,
)
from ticketing.models import BOX_RESET_CATEGORY_ID, Ticket, TicketCategory, TicketComment, TicketHistory


def test_normalize_ticket_host() -> None:
    assert normalize_ticket_host(5, "web") == "web"
    assert normalize_ticket_host(5, "team05-web") == "web"
    assert normalize_ticket_host(5, "WEB.wccomps.org") == "web"
    assert normalize_ticket_host(5, "  team05-dc-01.ad.local ") == "dc-01"
    assert normalize_ticket_host(5, "") is None
    assert normalize_ticket_host(5, "../etc/passwd") is None
    assert normalize_ticket_host(0, "web") is None
    assert normalize_ticket_host(100, "web") is None


def test_classify_team_vm_enforces_rangekiln_name_and_vmid_bounds() -> None:
    assert classify_team_vm(5, 10505, "team05-web") == "web"
    assert classify_team_vm(5, 10500, "team05-router") == "router"
    assert classify_team_vm(5, 10599, "team05-db-01") == "db-01"

    assert classify_team_vm(5, 10505, "team05-web", is_template=True) is None
    assert classify_team_vm(5, 10605, "team05-web") is None
    assert classify_team_vm(5, 9005, "team05-web") is None
    assert classify_team_vm(5, 2105, "team05-web") is None
    assert classify_team_vm(5, 10505, "team06-web") is None
    assert classify_team_vm(5, 10505, "web") is None
    assert classify_team_vm(5, 10505, "team05-../bad") is None


def test_is_valid_snapshot_name() -> None:
    assert is_valid_snapshot_name("clean") is True
    assert is_valid_snapshot_name("pre_comp-1.0") is True
    assert is_valid_snapshot_name("current") is False
    assert is_valid_snapshot_name("") is False
    assert is_valid_snapshot_name("../clean") is False
    assert is_valid_snapshot_name("clean/sub") is False


def test_match_team_vm_prevents_cross_team_selection() -> None:
    vms = [
        ProxmoxVM(vmid=10105, name="team01-web", host="web", node="pve01", status="running"),
        ProxmoxVM(vmid=10106, name="team01-dns", host="dns", node="pve01", status="stopped"),
    ]
    assert match_team_vm(vms, 1, "web") == vms[0]
    assert match_team_vm(vms, 1, "team01-dns.example.com") == vms[1]
    assert match_team_vm(vms, 1, "team02-web") is None


def test_proxmox_client_filters_pool_members_and_snapshots() -> None:
    client = ProxmoxClient(base_url="https://proxmox.test", api_token="user@pam!tok=secret")
    pool_payload = {
        "data": {
            "members": [
                {"type": "qemu", "vmid": 10505, "name": "team05-web", "node": "pve01", "status": "running"},
                {"type": "qemu", "vmid": 10605, "name": "team05-db", "node": "pve01", "status": "running"},
                {"type": "qemu", "vmid": 10506, "name": "team06-dns", "node": "pve01", "status": "running"},
                {
                    "type": "qemu",
                    "vmid": 10507,
                    "name": "team05-tmpl",
                    "node": "pve01",
                    "status": "stopped",
                    "template": 1,
                },
                {"type": "storage", "id": "storage/pve01/local"},
            ]
        }
    }
    snap_payload = {
        "data": [
            {"name": "pre-comp", "description": "Initial"},
            {"name": "clean", "description": "Clean baseline", "snaptime": 1700000000},
            {"name": "current", "description": "You are here!"},
        ]
    }
    with patch.object(client, "_request") as mock_req:
        resp_pool = MagicMock(status_code=200)
        resp_pool.json.return_value = pool_payload
        mock_req.return_value = resp_pool

        vms = client.list_team_vms(5)
        assert vms == [ProxmoxVM(vmid=10505, name="team05-web", host="web", node="pve01", status="running")]

        resp_snap = MagicMock(status_code=200)
        resp_snap.json.return_value = snap_payload
        mock_req.return_value = resp_snap

        snaps = client.list_snapshots("pve01", 10505)
        assert snaps == [
            ProxmoxSnapshot(name="pre-comp", description="Initial"),
            ProxmoxSnapshot(name="clean", description="Clean baseline"),
        ]


def test_proxmox_client_wait_for_task_polls_until_stopped() -> None:
    client = ProxmoxClient(base_url="https://proxmox.test", api_token="PVEAPIToken=user@pam!tok=secret")
    statuses = [
        ProxmoxTaskStatus(status="running", exitstatus=""),
        ProxmoxTaskStatus(status="stopped", exitstatus="OK"),
    ]
    with patch.object(client, "get_task_status", side_effect=statuses) as mock_status:
        result = client.wait_for_task("pve01", "UPID:pve01:1", timeout_seconds=5.0, poll_interval=0.0)
    assert result.ok is True
    assert mock_status.call_count == 2


def test_proxmox_client_wait_for_task_raises_on_error_exitstatus() -> None:
    client = ProxmoxClient(base_url="https://proxmox.test", api_token="user@pam!tok=secret")
    with (
        patch.object(
            client,
            "get_task_status",
            return_value=ProxmoxTaskStatus(status="stopped", exitstatus="rollback failed: lock timeout"),
        ),
        pytest.raises(ProxmoxAPIError, match="rollback failed"),
    ):
        client.wait_for_task("pve01", "UPID:pve01:1", timeout_seconds=1.0, poll_interval=0.0)


def test_proxmox_client_wait_for_task_already_running_requires_opt_in() -> None:
    client = ProxmoxClient(base_url="https://proxmox.test", api_token="user@pam!tok=secret")
    already_running = ProxmoxTaskStatus(status="stopped", exitstatus="VM 10105 already running")
    with patch.object(client, "get_task_status", return_value=already_running):
        with pytest.raises(ProxmoxAPIError, match="already running"):
            client.wait_for_task("pve01", "UPID:pve01:1", timeout_seconds=1.0, poll_interval=0.0)
        result = client.wait_for_task(
            "pve01",
            "UPID:pve01:1",
            timeout_seconds=1.0,
            poll_interval=0.0,
            allow_already_running=True,
        )
        assert result.finished is True


@pytest.mark.django_db
class TestTicketBoxResetViews:
    @pytest.fixture
    def box_reset_category(self) -> TicketCategory:
        cat, _ = TicketCategory.objects.update_or_create(
            id=BOX_RESET_CATEGORY_ID,
            defaults={
                "display_name": "Box Reset / Scrub",
                "points": 60,
                "required_fields": ["hostname", "ip_address"],
                "user_creatable": True,
            },
        )
        return cat

    @pytest.fixture
    def box_ticket(self, blue_team, box_reset_category: TicketCategory, ticketing_support_user) -> Ticket:
        return Ticket.objects.create(
            ticket_number="T001-010",
            team=blue_team,
            category=box_reset_category,
            title="Box Reset / Scrub",
            hostname="web",
            ip_address="192.168.100.5",
            status=Ticket.STATUS_CLAIMED,
            assigned_to=ticketing_support_user,
            points_charged=60,
        )

    def test_ticket_detail_includes_box_reset_panel_for_ops_only(
        self,
        client: Client,
        box_ticket: Ticket,
        ticketing_support_user,
        blue_team_user,
    ) -> None:
        url = reverse("ticket_detail", kwargs={"ticket_number": box_ticket.ticket_number})
        panel_url = reverse("ticket_box_reset_panel", kwargs={"ticket_number": box_ticket.ticket_number})

        client.force_login(ticketing_support_user)
        staff_resp = client.get(url)
        assert staff_resp.status_code == 200
        assert panel_url in staff_resp.content.decode()

        client.force_login(blue_team_user)
        student_resp = client.get(url)
        assert student_resp.status_code == 200
        body = student_resp.content.decode()
        assert panel_url not in body
        assert "box-reset-panel" not in body

    def test_box_reset_panel_renders_matched_vm_and_preselects_default_snapshot(
        self,
        client: Client,
        box_ticket: Ticket,
        ticketing_support_user,
    ) -> None:
        mock_client = MagicMock()
        mock_client.list_team_vms.return_value = [
            ProxmoxVM(vmid=10105, name="team01-web", host="web", node="pve01", status="running"),
        ]
        mock_client.list_snapshots.return_value = [
            ProxmoxSnapshot(name="initial", description="Initial build"),
            ProxmoxSnapshot(name="clean", description="Pre-competition clean state"),
        ]

        client.force_login(ticketing_support_user)
        url = reverse("ticket_box_reset_panel", kwargs={"ticket_number": box_ticket.ticket_number})
        with (
            override_settings(PROXMOX_DEFAULT_SNAPSHOT="clean"),
            patch("core.proxmox.get_proxmox_client", return_value=mock_client),
        ):
            resp = client.get(url)

        assert resp.status_code == 200
        html = resp.content.decode()
        assert "team01-web" in html
        assert "10105 on pve01" in html
        assert 'value="clean" selected' in " ".join(html.split())
        assert "Reset Box" in html

    def test_box_reset_panel_handles_proxmox_unreachable(
        self,
        client: Client,
        box_ticket: Ticket,
        ticketing_support_user,
    ) -> None:
        mock_client = MagicMock()
        mock_client.list_team_vms.return_value = None

        client.force_login(ticketing_support_user)
        url = reverse("ticket_box_reset_panel", kwargs={"ticket_number": box_ticket.ticket_number})
        with patch("core.proxmox.get_proxmox_client", return_value=mock_client):
            resp = client.get(url)

        assert resp.status_code == 200
        assert "Proxmox isn't reachable." in resp.content.decode()

    def test_box_reset_endpoints_deny_students(
        self,
        client: Client,
        box_ticket: Ticket,
        blue_team_user,
    ) -> None:
        client.force_login(blue_team_user)
        panel_url = reverse("ticket_box_reset_panel", kwargs={"ticket_number": box_ticket.ticket_number})
        run_url = reverse("ticket_box_reset", kwargs={"ticket_number": box_ticket.ticket_number})

        assert client.get(panel_url).status_code == 403
        assert client.post(run_url, {"snapshot": "clean"}).status_code == 403

    def test_box_reset_refuses_other_categories_without_calling_proxmox(
        self,
        client: Client,
        box_ticket: Ticket,
        ticketing_support_user,
    ) -> None:
        other = TicketCategory.objects.exclude(id=BOX_RESET_CATEGORY_ID).first() or TicketCategory.objects.create(
            id=BOX_RESET_CATEGORY_ID + 100, display_name="Other", points=0
        )
        Ticket.objects.filter(id=box_ticket.id).update(category=other)
        mock_client = MagicMock()

        client.force_login(ticketing_support_user)
        panel_url = reverse("ticket_box_reset_panel", kwargs={"ticket_number": box_ticket.ticket_number})
        run_url = reverse("ticket_box_reset", kwargs={"ticket_number": box_ticket.ticket_number})
        with patch("core.proxmox.get_proxmox_client", return_value=mock_client):
            assert client.get(panel_url).status_code == 404
            resp = client.post(run_url, {"snapshot": "clean"})
            lines = [json.loads(line) for line in b"".join(resp.streaming_content).decode().splitlines()]
        assert lines[-1]["success"] is False
        assert "only available on Box Reset tickets" in lines[-1]["message"]
        assert not mock_client.method_calls

    def test_box_reset_refuses_while_another_reset_holds_the_ticket(
        self,
        client: Client,
        box_ticket: Ticket,
        ticketing_support_user,
    ) -> None:
        cache.clear()
        cache.add(f"box_reset_lock_{box_ticket.id}", 0, timeout=60)
        mock_client = MagicMock()

        client.force_login(ticketing_support_user)
        run_url = reverse("ticket_box_reset", kwargs={"ticket_number": box_ticket.ticket_number})
        with patch("core.proxmox.get_proxmox_client", return_value=mock_client):
            resp = client.post(run_url, {"snapshot": "clean"})
            lines = [json.loads(line) for line in b"".join(resp.streaming_content).decode().splitlines()]
        cache.clear()
        assert lines[-1]["success"] is False
        assert "already running" in lines[-1]["message"]
        assert not mock_client.method_calls

    def test_box_reset_stream_rolls_back_starts_comments_audits_and_resolves(
        self,
        client: Client,
        box_ticket: Ticket,
        ticketing_support_user,
    ) -> None:
        cache.clear()
        mock_client = MagicMock()
        mock_client.list_team_vms.return_value = [
            ProxmoxVM(vmid=10105, name="team01-web", host="web", node="pve01", status="running"),
        ]
        mock_client.list_snapshots.return_value = [
            ProxmoxSnapshot(name="clean", description="Clean baseline"),
        ]
        mock_client.rollback_snapshot.return_value = "UPID:pve01:rollback1"
        mock_client.start_vm.return_value = "UPID:pve01:start1"
        mock_client.wait_for_task.return_value = ProxmoxTaskStatus(status="stopped", exitstatus="OK")

        client.force_login(ticketing_support_user)
        run_url = reverse("ticket_box_reset", kwargs={"ticket_number": box_ticket.ticket_number})
        with patch("core.proxmox.get_proxmox_client", return_value=mock_client):
            resp = client.post(run_url, {"snapshot": "clean"})
            assert resp.status_code == 200
            assert resp["Content-Type"] == "application/x-ndjson"
            lines = [
                json.loads(line) for line in b"".join(resp.streaming_content).decode().splitlines() if line.strip()
            ]
        assert lines[-1]["done"] is True
        assert lines[-1]["success"] is True

        mock_client.rollback_snapshot.assert_called_once_with("pve01", 10105, "clean")
        mock_client.start_vm.assert_called_once_with("pve01", 10105)
        assert mock_client.wait_for_task.call_count == 2

        box_ticket.refresh_from_db()
        assert box_ticket.status == Ticket.STATUS_RESOLVED
        assert box_ticket.points_charged == 60
        assert box_ticket.resolution_notes == "Reset team01-web to snapshot 'clean'."

        comments = list(TicketComment.objects.filter(ticket=box_ticket).order_by("id"))
        assert len(comments) == 2
        assert comments[0].comment_text == "Starting box reset of team01-web to snapshot 'clean'."
        assert comments[1].comment_text == "Box reset of team01-web to snapshot 'clean' complete; VM is started."
        assert DiscordTask.objects.filter(ticket=box_ticket, task_type="post_comment").count() == 2

        history = TicketHistory.objects.filter(ticket=box_ticket, action="box_reset").first()
        assert history is not None
        assert history.actor == ticketing_support_user
        assert history.details["vm_name"] == "team01-web"
        assert history.details["snapshot"] == "clean"
        assert history.details["rollback_upid"] == "UPID:pve01:rollback1"
        assert history.details["start_upid"] == "UPID:pve01:start1"

        audit = AuditLog.objects.filter(action="box_reset", target_id=box_ticket.id).first()
        assert audit is not None
        assert audit.details["result"] == "ok"
        assert audit.details["vmid"] == 10105

    def test_box_reset_failure_records_history_and_audit_without_resolving(
        self,
        client: Client,
        box_ticket: Ticket,
        ticketing_support_user,
    ) -> None:
        cache.clear()
        mock_client = MagicMock()
        mock_client.list_team_vms.return_value = [
            ProxmoxVM(vmid=10105, name="team01-web", host="web", node="pve01", status="running"),
        ]
        mock_client.list_snapshots.return_value = [
            ProxmoxSnapshot(name="clean", description="Clean baseline"),
        ]
        mock_client.rollback_snapshot.return_value = "UPID:pve01:rollback1"
        mock_client.wait_for_task.side_effect = ProxmoxAPIError("Proxmox task failed: storage lock")

        client.force_login(ticketing_support_user)
        run_url = reverse("ticket_box_reset", kwargs={"ticket_number": box_ticket.ticket_number})
        with patch("core.proxmox.get_proxmox_client", return_value=mock_client):
            resp = client.post(run_url, {"snapshot": "clean"})
            lines = [
                json.loads(line) for line in b"".join(resp.streaming_content).decode().splitlines() if line.strip()
            ]
        assert lines[-1]["done"] is True
        assert lines[-1]["success"] is False
        assert "storage lock" in lines[-1]["message"]

        box_ticket.refresh_from_db()
        assert box_ticket.status == Ticket.STATUS_CLAIMED

        history = TicketHistory.objects.filter(ticket=box_ticket, action="box_reset_failed").first()
        assert history is not None
        assert history.details["result"] == "failed"

        audit = AuditLog.objects.filter(action="box_reset", target_id=box_ticket.id).first()
        assert audit is not None
        assert audit.details["result"] == "failed"
