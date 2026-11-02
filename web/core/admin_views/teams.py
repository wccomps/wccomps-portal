from typing import cast

from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from core.auth_utils import require_permission
from core.authentik_utils import Changed, Refused, reset_team, reset_team_credentials
from core.discord_tasks import SetupTeamInfrastructure, SyncMemberRoles
from core.errors import json_endpoint
from core.forms import TeamActionForm, TeamsBulkActionForm
from core.models import AuditLog, DiscordTask
from core.services.linking import ADMIN_UNLINK, unlink_discord_links
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

        links = DiscordLink.objects.filter(discord_id=discord_id, team=team)
        ended = unlink_discord_links(links, actor=authentik_username, reason=ADMIN_UNLINK)
        if not ended:
            raise BadRequest("User not found or not linked to this team")
        return JsonResponse({"success": True, "message": f"User {ended[0].discord_username} unlinked"})

    elif action == "reset":
        reset = reset_team(team_number, actor=authentik_username, recreate_channels=True).credentials
        match reset:
            case Changed(password=password):
                return JsonResponse(
                    {
                        "success": reset.complete,
                        "message": f"Team reset: {reset.describe()}",
                        "password": password,
                        "members_unlinked": reset.members_unlinked,
                    }
                )
            case Refused():
                return JsonResponse(
                    {
                        "success": False,
                        "message": f"Team partially reset: {reset.describe()}",
                        "members_unlinked": reset.members_unlinked,
                    }
                )

    elif action == "reset_password":
        reset = reset_team_credentials(team_number, actor=authentik_username)
        match reset:
            case Changed(password=password):
                return JsonResponse(
                    {
                        "success": reset.complete,
                        "message": f"New password set in Authentik; {reset.describe()}",
                        "password": password,
                        "members_unlinked": reset.members_unlinked,
                    }
                )
            case Refused(error=error):
                return JsonResponse({"success": False, "message": f"Password reset failed: {error}"})

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
        with transaction.atomic():
            locked = Team.objects.filter(team_number__in=team_numbers).lock()
            updated = Team.objects.filter(pk__in=locked).update(is_active=target_active)
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
