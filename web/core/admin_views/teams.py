from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.auth_utils import require_permission
from core.authentik_utils import reset_team_credentials
from core.discord_tasks import SetupTeamInfrastructure, SyncMemberRoles
from core.errors import json_endpoint
from core.forms import TeamActionForm, TeamsBulkActionForm
from core.models import AuditLog, DiscordTask
from team.models import DiscordLink, Team


@require_permission("admin", "gold_team")
def admin_teams(request: HttpRequest) -> HttpResponse:
    teams_with_info = [
        {
            "team": team,
            "member_count": team.get_member_count(),
        }
        for team in Team.objects.with_active_member_count()
    ]

    return render(request, "admin/teams.html", {"teams": teams_with_info})


@require_permission("admin", "gold_team")
def admin_team_detail(request: HttpRequest, team_number: int) -> HttpResponse:
    team = get_object_or_404(Team, team_number=team_number)

    members = DiscordLink.objects.filter(team=team, is_active=True).order_by("linked_at")

    context = {
        "team": team,
        "members": members,
    }

    return render(request, "admin/team_detail.html", context)


@json_endpoint
@require_POST
@require_permission("admin", "gold_team")
def admin_team_action(request: HttpRequest, team_number: int) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username
    team = get_object_or_404(Team, team_number=team_number)

    form = TeamActionForm(request.POST)
    if not form.is_valid():
        raise BadRequest("Invalid action")

    action = form.cleaned_data["action"]

    if action in ("activate", "deactivate"):
        target_active = action == "activate"
        team.is_active = target_active
        team.save(update_fields=["is_active"])
        for member_discord_id in DiscordLink.objects.filter(team=team, is_active=True).values_list(
            "discord_id", flat=True
        ):
            DiscordTask.enqueue(SyncMemberRoles(discord_id=member_discord_id))
        AuditLog.objects.create(
            action="team_activated" if target_active else "team_deactivated",
            admin_user=authentik_username,
            target_entity="team",
            target_id=team_number,
            details={"team_name": team.team_name},
        )
        msg = (
            f"Team {team_number} activated; its members get their team roles within a few minutes"
            if target_active
            else f"Team {team_number} deactivated; its members lose their team roles within a few minutes"
        )
        return JsonResponse({"success": True, "message": msg})

    elif action == "unlink_user":
        discord_id = form.cleaned_data.get("discord_id")
        if not discord_id:
            raise BadRequest("Discord ID required")

        try:
            link = DiscordLink.objects.get(discord_id=discord_id, team=team, is_active=True)
            link.is_active = False
            link.unlinked_at = timezone.now()
            link.save()
            DiscordTask.enqueue(SyncMemberRoles(discord_id=link.discord_id))

            AuditLog.objects.create(
                action="user_unlinked",
                admin_user=authentik_username,
                target_entity="discord_link",
                target_id=discord_id,
                details={
                    "discord_username": link.discord_username,
                    "team_name": team.team_name,
                    "authentik_username": link.user.username,
                },
            )

            return JsonResponse({"success": True, "message": f"User {link.discord_username} unlinked"})
        except DiscordLink.DoesNotExist:
            raise BadRequest("User not found or not linked to this team") from None

    elif action == "reset":
        links = DiscordLink.objects.filter(team=team, is_active=True).select_related("user")
        unlinked = 0
        for link in links:
            link.is_active = False
            link.unlinked_at = timezone.now()
            link.save()
            DiscordTask.enqueue(SyncMemberRoles(discord_id=link.discord_id))
            AuditLog.objects.create(
                action="user_unlinked",
                admin_user=authentik_username,
                target_entity="discord_link",
                target_id=link.discord_id,
                details={
                    "discord_id": link.discord_id,
                    "discord_username": link.discord_username,
                    "team_name": team.team_name,
                    "authentik_username": link.user.username,
                    "reason": "team_reset",
                },
            )
            unlinked += 1

        DiscordTask.enqueue(SetupTeamInfrastructure(team_number=team_number, recreate=True))

        password, error, sessions_revoked = reset_team_credentials(team_number)
        success = password is not None

        AuditLog.objects.create(
            action="team_reset",
            admin_user=authentik_username,
            target_entity="team",
            target_id=team_number,
            details={
                "team_name": team.team_name,
                "unlinked_members": unlinked,
                "password_reset": success,
                "sessions_revoked": sessions_revoked,
                "recreated_channels": True,
            },
        )

        if success:
            return JsonResponse(
                {
                    "success": True,
                    "message": f"Team reset: {unlinked} unlinked, {sessions_revoked} sessions revoked",
                    "password": password,
                }
            )
        else:
            return JsonResponse(
                {
                    "success": False,
                    "message": f"Team partially reset: {unlinked} unlinked, but password reset failed: {error}",
                }
            )

    elif action == "reset_password":
        # A new password only: Discord links stay, unlike "reset"
        password, error, sessions_revoked = reset_team_credentials(team_number)
        AuditLog.objects.create(
            action="team_password_reset",
            admin_user=authentik_username,
            target_entity="team",
            target_id=team_number,
            details={
                "team_name": team.team_name,
                "password_reset": password is not None,
                "sessions_revoked": sessions_revoked,
            },
        )
        if password is None:
            return JsonResponse({"success": False, "message": f"Password reset failed: {error}"})
        message = "New password set in Authentik"
        message += "; asked Authentik to end its sessions" if sessions_revoked else "; could not end its sessions"
        return JsonResponse({"success": True, "message": message, "password": password})

    elif action == "recreate_channels":
        DiscordTask.enqueue(SetupTeamInfrastructure(team_number=team_number, recreate=True))

        AuditLog.objects.create(
            action="team_channels_recreate_requested",
            admin_user=authentik_username,
            target_entity="team",
            target_id=team_number,
            details={"team_name": team.team_name},
        )

        return JsonResponse({"success": True, "message": "Channel recreation task queued for Discord bot"})

    raise BadRequest("Unknown action")


@json_endpoint
@require_POST
@require_permission("admin", "gold_team")
def admin_teams_bulk_action(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    authentik_username = user.username

    form = TeamsBulkActionForm(request.POST)
    if not form.is_valid():
        error_msg = "; ".join(str(e) for errors in form.errors.values() for e in errors)
        raise BadRequest(error_msg or "Invalid input")

    action = form.cleaned_data["action"]
    team_numbers = form.cleaned_data["team_numbers"]

    if action in ("activate", "deactivate"):
        target_active = action == "activate"
        updated = Team.objects.filter(team_number__in=team_numbers).update(is_active=target_active)
        for discord_id in DiscordLink.objects.filter(team__team_number__in=team_numbers, is_active=True).values_list(
            "discord_id", flat=True
        ):
            DiscordTask.enqueue(SyncMemberRoles(discord_id=discord_id))
        AuditLog.objects.create(
            action="teams_activated" if target_active else "teams_deactivated",
            admin_user=authentik_username,
            target_entity="teams",
            target_id=0,
            details={"team_numbers": team_numbers, "updated_count": updated},
        )
        msg = (
            f"Activated {updated} teams; their members get their team roles within a few minutes"
            if target_active
            else f"Deactivated {updated} teams; their members lose their team roles within a few minutes"
        )
        return JsonResponse({"success": True, "message": msg})

    elif action == "recreate":
        for team_number in team_numbers:
            DiscordTask.enqueue(SetupTeamInfrastructure(team_number=team_number, recreate=True))

        AuditLog.objects.create(
            action="teams_recreate_requested",
            admin_user=authentik_username,
            target_entity="teams",
            target_id=0,
            details={"team_numbers": team_numbers},
        )

        return JsonResponse({"success": True, "message": f"Queued {len(team_numbers)} channel recreation tasks"})

    raise BadRequest("Unknown action")
