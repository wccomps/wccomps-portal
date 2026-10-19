import logging
from collections.abc import Iterator
from typing import cast

from django.contrib import messages
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.auth_utils import require_permission, team_record_or_404
from core.utils import ndjson_done, ndjson_progress, run_detached
from ticketing.access import TICKET_STAFF
from ticketing.forms import (
    TicketBoxResetForm,
    TicketChangeCategoryForm,
    TicketReassignForm,
    TicketReopenForm,
    TicketResolveForm,
)
from ticketing.lifecycle import (
    assign_ticket,
    assignee_refusal,
    cancel_ticket,
    claim_ticket,
    refusal,
    reopen_ticket,
    resolve_ticket,
    unclaim_ticket,
)
from ticketing.models import BOX_RESET_CATEGORY_ID, Ticket

from .list import back_to_list

logger = logging.getLogger(__name__)


@require_POST
def ticket_cancel(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Cancel an open ticket (team members only)."""
    user = cast(User, request.user)
    authentik_username = user.username
    # A team's own: staff file and work tickets but don't cancel them
    ticket_obj = team_record_or_404(Ticket.objects.select_related("team"), user, staff=(), ticket_number=ticket_number)

    ticket, error = cancel_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to cancel ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} cancelled by {authentik_username} via web")

    return redirect("ticket_list")


@require_POST
@require_permission(TICKET_STAFF)
def ticket_claim(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Claim a ticket (ticketing staff only)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    ticket, error = claim_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to claim ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} claimed by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_unclaim(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Put a claimed ticket back in the open queue (its assignee or a ticketing admin: the lifecycle checks)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    ticket, error = unclaim_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to unclaim ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} unclaimed by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_reassign(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Give a ticket to a staff member: any ticketing staff for an open one, its assignee or a ticketing admin for a
    ticket someone holds (the lifecycle checks)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketReassignForm(request.POST)
    if not form.is_valid():
        messages.error(request, "New assignee username is required")
        return redirect("ticket_detail", ticket_number=ticket_number)
    new_assignee_username = form.cleaned_data["new_assignee_username"]

    new_assignee_user = User.objects.filter(username=new_assignee_username).first()
    if not new_assignee_user:
        messages.error(request, f"User '{new_assignee_username}' not found")
        return redirect("ticket_detail", ticket_number=ticket_number)

    ticket, error = assign_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        user=new_assignee_user,
        actor=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to assign ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} reassigned to {new_assignee_username} by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_resolve(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Resolve a ticket (its assignee or a ticketing admin: the lifecycle checks)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketResolveForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid form data. Check points value.")
        return redirect("ticket_detail", ticket_number=ticket_number)

    resolution_notes = form.cleaned_data["resolution_notes"]
    points_override = form.cleaned_data.get("points_override")

    ticket, error = resolve_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        resolution_notes=resolution_notes,
        points_override=points_override,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to resolve ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} resolved by {authentik_username}")
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission("ticketing_admin")
def ticket_reopen(request: HttpRequest, ticket_number: str) -> HttpResponse:
    """Reopen a resolved ticket (ticketing admins)."""
    user = cast(User, request.user)
    authentik_username = user.username

    ticket_obj = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketReopenForm(request.POST)
    form.is_valid()  # Always valid (optional field)
    reopen_reason = form.cleaned_data.get("reopen_reason", "")

    ticket, error = reopen_ticket(
        ticket_id=ticket_obj.id,
        actor_username=authentik_username,
        reopen_reason=reopen_reason,
        user=user,
    )

    if error or ticket is None:
        messages.error(request, error or "Failed to reopen ticket")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(
        f"Ticket {ticket_number} reopened by {authentik_username}" + (f": {reopen_reason}" if reopen_reason else "")
    )
    return back_to_list(request, reverse("ticket_detail", args=[ticket_number]))


@require_POST
@require_permission(TICKET_STAFF)
def ticket_change_category(request: HttpRequest, ticket_number: str) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    ticket = get_object_or_404(Ticket.objects.select_related("team"), ticket_number=ticket_number)

    form = TicketChangeCategoryForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Invalid category")
        return redirect("ticket_detail", ticket_number=ticket_number)
    new_category_id = form.cleaned_data["new_category"]

    from ticketing.utils import change_ticket_category_atomic

    changed, error = change_ticket_category_atomic(
        ticket_id=ticket.id, new_category_id=new_category_id, actor_username=authentik_username, user=user
    )
    if error or changed is None:
        messages.error(request, error or "Failed to change category")
        return redirect("ticket_detail", ticket_number=ticket_number)

    logger.info(f"Ticket {ticket_number} category changed by {authentik_username} to category {new_category_id}")

    return redirect("ticket_detail", ticket_number=ticket_number)


def _record_box_reset_outcome(
    *,
    ticket: Ticket,
    user: User,
    authentik_username: str,
    vm_name: str,
    vmid: int,
    node: str,
    snapshot: str,
    rollback_upid: str,
    start_upid: str,
    error: str = "",
) -> None:
    from core.models import AuditLog
    from ticketing.models import TicketHistory

    result = "failed" if error else "ok"
    common: dict[str, object] = {
        "vm_name": vm_name,
        "vmid": vmid,
        "node": node,
        "snapshot": snapshot,
        "rollback_upid": rollback_upid,
        "start_upid": start_upid,
        "result": result,
    }
    if error:
        common["error"] = error
    TicketHistory.objects.create(
        ticket=ticket,
        action="box_reset_failed" if error else "box_reset",
        actor=user,
        details={"reset_by": authentik_username, **common},
    )
    AuditLog.objects.create(
        action="box_reset",
        admin_user=authentik_username,
        target_entity="ticket",
        target_id=ticket.id,
        details={
            "ticket_number": ticket.ticket_number,
            "team_number": ticket.team.team_number,
            **common,
        },
    )


def _stream_box_reset(ticket_id: int, user_id: int, authentik_username: str, snapshot: str) -> Iterator[str]:
    from django.core.cache import cache

    from core.discord_tasks import PostComment
    from core.models import DiscordTask
    from core.proxmox import ProxmoxAPIError, get_proxmox_client, match_team_vm, normalize_ticket_host
    from ticketing.models import TicketComment

    # Held across the state check and the reset, so a second submission sees the resolved ticket
    lock_key = f"box_reset_lock_{ticket_id}"
    if not cache.add(lock_key, user_id, timeout=300):
        yield ndjson_done("A box reset is already running for this ticket.", success=False)
        return

    try:
        ticket = Ticket.objects.select_related("team", "assigned_to").filter(id=ticket_id).first()
        user = User.objects.filter(id=user_id).first()
        if not ticket or not user:
            yield ndjson_done("Ticket or user not found.", success=False)
            return

        if ticket.category_id != BOX_RESET_CATEGORY_ID:
            yield ndjson_done("Box reset is only available on Box Reset tickets.", success=False)
            return

        if error := refusal(ticket, "resolve") or assignee_refusal(ticket, user, "resolve"):
            yield ndjson_done(error, success=False)
            return

        if normalize_ticket_host(ticket.team.team_number, ticket.hostname) is None:
            yield ndjson_done(
                f"No Proxmox VM matched hostname '{ticket.hostname}' for Team {ticket.team.team_number}.",
                success=False,
            )
            return

        client = get_proxmox_client()
        vms = client.list_team_vms(ticket.team.team_number)
        if vms is None:
            yield ndjson_done("Proxmox isn't reachable.", success=False)
            return

        vm = match_team_vm(vms, ticket.team.team_number, ticket.hostname)
        if vm is None:
            yield ndjson_done(
                f"No Proxmox VM matched hostname '{ticket.hostname}' for Team {ticket.team.team_number}.",
                success=False,
            )
            return

        snapshots = client.list_snapshots(vm.node, vm.vmid)
        if snapshots is None:
            yield ndjson_done("Proxmox isn't reachable.", success=False)
            return
        if snapshot not in {s.name for s in snapshots}:
            yield ndjson_done(f"Snapshot '{snapshot}' not found on {vm.name}.", success=False)
            return

        rollback_upid = ""
        start_upid = ""
        try:
            start_comment = TicketComment.objects.create(
                ticket=ticket,
                author=user,
                comment_text=f"Starting box reset of {vm.name} to snapshot '{snapshot}'.",
            )
            DiscordTask.enqueue(PostComment(ticket_id=ticket.id, comment_id=start_comment.id))

            yield ndjson_progress(f"Rolling back {vm.name} to '{snapshot}'...", 1, 4)
            rollback_upid = client.rollback_snapshot(vm.node, vm.vmid, snapshot)

            yield ndjson_progress(f"Waiting for rollback of {vm.name}...", 2, 4)
            client.wait_for_task(vm.node, rollback_upid)

            yield ndjson_progress(f"Starting {vm.name}...", 3, 4)
            start_upid = client.start_vm(vm.node, vm.vmid)
            client.wait_for_task(vm.node, start_upid, allow_already_running=True)

            yield ndjson_progress(f"Resolving ticket {ticket.ticket_number}...", 4, 4)
            _, resolve_err = resolve_ticket(
                ticket_id=ticket.id,
                actor_username=authentik_username,
                resolution_notes=f"Reset {vm.name} to snapshot '{snapshot}'.",
                user=user,
            )
            if resolve_err:
                _record_box_reset_outcome(
                    ticket=ticket,
                    user=user,
                    authentik_username=authentik_username,
                    vm_name=vm.name,
                    vmid=vm.vmid,
                    node=vm.node,
                    snapshot=snapshot,
                    rollback_upid=rollback_upid,
                    start_upid=start_upid,
                    error=resolve_err,
                )
                yield ndjson_done(f"VM reset completed, but ticket could not be resolved: {resolve_err}", success=False)
                return

            done_comment = TicketComment.objects.create(
                ticket=ticket,
                author=user,
                comment_text=f"Box reset of {vm.name} to snapshot '{snapshot}' complete; VM is started.",
            )
            DiscordTask.enqueue(PostComment(ticket_id=ticket.id, comment_id=done_comment.id))
            _record_box_reset_outcome(
                ticket=ticket,
                user=user,
                authentik_username=authentik_username,
                vm_name=vm.name,
                vmid=vm.vmid,
                node=vm.node,
                snapshot=snapshot,
                rollback_upid=rollback_upid,
                start_upid=start_upid,
            )

            logger.info(
                f"Box reset of {vm.name} to '{snapshot}' completed on {ticket.ticket_number} by {authentik_username}"
            )
            yield ndjson_done(f"Reset {vm.name} to '{snapshot}' and resolved {ticket.ticket_number}.")
        except ProxmoxAPIError as e:
            _record_box_reset_outcome(
                ticket=ticket,
                user=user,
                authentik_username=authentik_username,
                vm_name=vm.name,
                vmid=vm.vmid,
                node=vm.node,
                snapshot=snapshot,
                rollback_upid=rollback_upid,
                start_upid=start_upid,
                error=str(e),
            )
            yield ndjson_done(f"Box reset error: {e}", success=False)
    finally:
        cache.delete(lock_key)


@require_POST
@require_permission(TICKET_STAFF)
def ticket_box_reset(request: HttpRequest, ticket_number: str) -> StreamingHttpResponse:
    """Roll back and start the matched Proxmox VM for a Box Reset ticket, then resolve the ticket."""
    user = cast(User, request.user)
    ticket = get_object_or_404(Ticket, ticket_number=ticket_number)

    form = TicketBoxResetForm(request.POST)
    if not form.is_valid():
        return StreamingHttpResponse(
            [ndjson_done("Invalid snapshot name.", success=False)],
            content_type="application/x-ndjson",
        )

    return StreamingHttpResponse(
        run_detached(_stream_box_reset(ticket.id, user.id, user.username, form.cleaned_data["snapshot"])),
        content_type="application/x-ndjson",
    )
