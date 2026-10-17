from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from core.auth_utils import require_permission
from core.discord_tasks import BroadcastMessage, SyncRoles, SyncRolesResult
from core.errors import json_endpoint
from core.forms import BroadcastForm, SyncRolesForm
from core.models import AuditLog, DiscordTask
from core.utils import role_sync_summary
from team.models import Team


@require_permission("admin", "gold_team")
def admin_broadcast(request: HttpRequest) -> HttpResponse:
    teams = Team.objects.filter(is_active=True).order_by("team_number")

    context = {
        "teams": teams,
        "show_ops_nav": True,
        "nav_active": "ops_admin",
    }

    return render(request, "admin/broadcast.html", context)


@json_endpoint
@require_POST
@require_permission("admin", "gold_team")
def admin_broadcast_action(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    form = BroadcastForm(request.POST)
    if not form.is_valid():
        raise BadRequest("Target and message required")

    target = form.cleaned_data["target"]
    message = form.cleaned_data["message"]

    DiscordTask.enqueue(BroadcastMessage(target=target, message=message, sender=authentik_username))

    AuditLog.objects.create(
        action="broadcast_message",
        admin_user=authentik_username,
        target_entity="broadcast",
        target_id=0,
        details={
            "target": target,
            "message_preview": message[:200],
        },
    )

    return JsonResponse({"success": True, "message": "Broadcast task queued for Discord bot"})


@require_permission("admin", "gold_team")
def admin_sync_roles(request: HttpRequest) -> HttpResponse:
    context = {
        "show_ops_nav": True,
        "nav_active": "ops_admin",
    }

    return render(request, "admin/sync_roles.html", context)


@json_endpoint
@require_POST
@require_permission("admin", "gold_team")
def admin_sync_roles_action(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    form = SyncRolesForm(request.POST)
    dry_run = form.cleaned_data["dry_run"] if form.is_valid() else True

    task = DiscordTask.enqueue(SyncRoles(requested_by=authentik_username, dry_run=dry_run))

    AuditLog.objects.create(
        action="role_sync_started",
        admin_user=authentik_username,
        target_entity="guilds",
        target_id=0,
        details={"source": "web", "task_id": task.id},
    )

    return JsonResponse(
        {
            "success": True,
            "task_id": task.id,
            "message": "Role sync started...",
        }
    )


@json_endpoint
@require_permission("admin", "gold_team")
def admin_task_status(request: HttpRequest, task_id: int) -> HttpResponse:
    task = get_object_or_404(DiscordTask, id=task_id)

    response: dict[str, object] = {
        "status": task.status,
        "task_type": task.task_type,
    }

    if task.status == "completed":
        if task.task_type == "sync_roles":
            result = cast(SyncRolesResult, task.result)
            response["message"] = role_sync_summary(result, dry_run=cast(SyncRoles, task.typed_payload()).dry_run)
            response["changes"] = result["changes"]
        else:
            response["message"] = "Task completed"
    elif task.status == "failed":
        response["message"] = task.error_message or "Task failed"
    else:
        response["message"] = "Processing..."

    return JsonResponse(response)
