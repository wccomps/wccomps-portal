"""Service layer for Discord-Authentik account linking."""

import logging
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction

from core.discord_tasks import LogToChannel, SetupTeamInfrastructure, SyncMemberRoles
from core.models import DiscordTask
from team.models import DiscordLink, LinkAttempt, LinkToken, Team

logger = logging.getLogger(__name__)


@dataclass
class LinkResult:
    success: bool
    error_template: str | None = None
    error_context: dict[str, str] | None = None


def lookup_valid_link_token(token: str, *, for_update: bool = False) -> LinkResult | LinkToken:
    """Look up an unused LinkToken and verify it has not expired."""
    tokens = LinkToken.objects.select_for_update() if for_update else LinkToken.objects
    try:
        link_token = tokens.get(token=token, used=False)
    except LinkToken.DoesNotExist:
        return LinkResult(
            success=False,
            error_template="link_error.html",
            error_context={
                "error": "Invalid or expired token",
                "message": (
                    "This link has expired or been used already. Please use /link in Discord to generate a new one."
                ),
            },
        )

    if link_token.is_expired():
        return LinkResult(
            success=False,
            error_template="link_error.html",
            error_context={
                "error": "Token expired",
                "message": (
                    "This link has expired (15 minute limit). Please use /link in Discord to generate a new one."
                ),
            },
        )

    return link_token


def validate_link_token(
    url_token: str | None,
    session_token: str | None,
    username: str,
    *,
    for_update: bool = False,
) -> LinkResult | LinkToken:
    """Validate the link token from URL and session, returning the LinkToken or an error LinkResult."""
    if not url_token:
        return LinkResult(
            success=False,
            error_template="link_error.html",
            error_context={
                "error": "Invalid request",
                "message": (
                    "Missing authentication state. Please start the linking process again with /link in Discord."
                ),
            },
        )

    # The browser completing the link must be the one that opened /auth/link. Without this, a
    # logged-in user sent straight to /auth/link-callback?token=<someone else's> got that person's
    # Discord account linked to their own portal account (and roles/permissions with it).
    if not session_token or session_token != url_token:
        session_hint = f"'{session_token[:8]}...'" if session_token else "none"
        logger.warning(
            f"Link token not started in this browser: session {session_hint}, url '{url_token[:8]}...' for {username}"
        )
        return LinkResult(
            success=False,
            error_template="link_error.html",
            error_context={
                "error": "This link wasn't opened in this browser",
                "message": (
                    "This page was opened in a different browser from the one that started the link. Each team "
                    "member runs /link in Discord and finishes in the browser it opens, without copying the "
                    "address from another browser."
                ),
            },
        )

    return lookup_valid_link_token(url_token, for_update=for_update)


def enforce_account_link_policy(
    user: User,
    discord_id: int,
    discord_username: str,
    authentik_username: str,
    team: Team | None,
) -> LinkResult | None:
    """Check if account linking is allowed by policy, returning an error LinkResult if blocked, else None."""
    if team:
        return None

    existing_link = DiscordLink.objects.filter(user=user, is_active=True).first()
    if existing_link and existing_link.discord_id != discord_id:
        LinkAttempt.objects.create(
            discord_id=discord_id,
            discord_username=discord_username,
            authentik_username=authentik_username,
            team=team,
            success=False,
            failure_reason=f"Authentik account already linked to Discord user {existing_link.discord_username}",
        )
        return LinkResult(
            success=False,
            error_template="link_error.html",
            error_context={
                "error": "Account already linked",
                "message": (
                    f"This Authentik account ({authentik_username}) is already linked to "
                    f"Discord user {existing_link.discord_username}. "
                    "Each Authentik account can only be linked to one Discord account at a time. "
                    "Please contact an administrator if you need to unlink the previous account."
                ),
            },
        )
    return None


def store_discord_id_in_authentik(username: str, discord_id: int, authentik_id: str) -> None:
    """Optionally store discord_id in Authentik user attributes. Failures are non-fatal."""
    from core.authentik_manager import AuthentikManager

    with AuthentikManager() as manager:
        stored = manager.update_user_discord_id(username, discord_id, authentik_id)
    if stored:
        logger.info(f"Stored discord_id {discord_id} in Authentik for user {username}")
    else:
        logger.warning(f"Could not store discord_id in Authentik for {username}; it stays in DiscordLink only")


def execute_link(
    discord_id: int,
    discord_username: str,
    user: User,
    team: Team | None,
) -> LinkResult | None:
    """Create the DiscordLink, locking the team row to enforce fullness; returns an error LinkResult or None."""
    if team:
        with transaction.atomic():
            team = Team.objects.select_for_update().get(pk=team.pk)
            relinking = team.members.filter(discord_id=discord_id, is_active=True).exists()
            if not relinking and team.is_full():
                LinkAttempt.objects.create(
                    discord_id=discord_id,
                    discord_username=discord_username,
                    authentik_username=user.username,
                    team=team,
                    success=False,
                    failure_reason=f"Team full ({team.get_member_count()}/{team.max_members})",
                )
                return LinkResult(
                    success=False,
                    error_template="link_error.html",
                    error_context={
                        "error": "Team full",
                        "message": (
                            f"{team.team_name} is full ({team.get_member_count()}/{team.max_members} members). "
                            "Please contact an administrator."
                        ),
                    },
                )
            _create_link(discord_id, discord_username, user, team)
    else:
        _create_link(discord_id, discord_username, user, team=None)
    return None


def _create_link(
    discord_id: int,
    discord_username: str,
    user: User,
    team: Team | None,
) -> DiscordLink:
    """Create a DiscordLink, deactivating any previous link for this discord_id."""
    DiscordLink.deactivate_previous_links(discord_id)
    return DiscordLink.objects.create(
        discord_id=discord_id,
        discord_username=discord_username,
        user=user,
        team=team,
        is_active=True,
    )


def finalize_link(
    link_token: LinkToken,
    discord_id: int,
    discord_username: str,
    authentik_username: str,
    team: Team | None,
) -> None:
    """Mark token used, create audit records, and queue Discord tasks."""
    try:
        token_obj = LinkToken.objects.get(token=link_token.token)
        token_obj.used = True
        token_obj.save()
    except LinkToken.DoesNotExist:
        logger.warning(f"LinkToken disappeared during linking flow: token={link_token.token[:8]}...")

    LinkAttempt.objects.create(
        discord_id=discord_id,
        discord_username=discord_username,
        authentik_username=authentik_username,
        team=team,
        success=True,
        failure_reason="",
    )

    # Queued in this order so the team's role usually exists by the time the member sync runs; if the setup is
    # retried, the member gets Blueteam now and the team role from the periodic role sync within 5 minutes
    if team and team.is_active:
        DiscordTask.enqueue(SetupTeamInfrastructure(team_number=team.team_number))
    DiscordTask.enqueue(SyncMemberRoles(discord_id=discord_id))

    if team:
        DiscordTask.enqueue(
            LogToChannel(message=f"User Linked: <@{discord_id}> ({discord_username}) to **{team.team_name}**")
        )
        logger.info(f"Successfully linked {discord_username} ({discord_id}) to {team.team_name}")
    else:
        DiscordTask.enqueue(
            LogToChannel(
                message=f"User Linked: <@{discord_id}> ({discord_username}) to **{authentik_username}** (non-team)"
            )
        )
        logger.info(f"Successfully linked {discord_username} ({discord_id}) to {authentik_username}")
