"""Team linking commands (/link)."""

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from django.conf import settings
from django.utils import timezone

from team.models import DiscordLink, LinkRateLimit, LinkToken

logger = logging.getLogger(__name__)

RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW_HOURS = 1
TOKEN_EXPIRY_MINUTES = 15


@dataclass
class LinkCheckResult:
    """Result of checking if a user can link."""

    can_link: bool
    error_message: str = ""
    existing_team_name: str = ""


async def check_rate_limit(discord_id: int) -> tuple[bool, int]:
    """Check rate limit for link attempts. Returns (is_allowed, attempt_count)."""
    one_hour_ago = timezone.now() - timedelta(hours=RATE_LIMIT_WINDOW_HOURS)
    recent_attempts = await LinkRateLimit.objects.filter(discord_id=discord_id, attempted_at__gte=one_hour_ago).acount()
    return recent_attempts < RATE_LIMIT_MAX, recent_attempts


async def check_existing_link(discord_id: int) -> LinkCheckResult:
    """A user linked to a team must ask an admin to move; anyone else may (re)link.

    A link without a team is a staff link. It stays active until the new link is made,
    so abandoning the flow doesn't unlink anyone.
    """
    existing_link = await (
        DiscordLink.objects.filter(discord_id=discord_id, is_active=True).select_related("team").afirst()
    )

    if not existing_link or not existing_link.team:
        return LinkCheckResult(can_link=True)

    return LinkCheckResult(
        can_link=False,
        existing_team_name=existing_link.team.team_name,
        error_message=f"You are already linked to **{existing_link.team.team_name}**. "
        f"If you need to change teams, please contact an administrator.",
    )


async def create_link_token(discord_id: int, discord_username: str) -> str:
    """Create a link token and return the auth URL."""
    token = secrets.token_urlsafe(32)
    expires_at = timezone.now() + timedelta(minutes=TOKEN_EXPIRY_MINUTES)

    await LinkToken.objects.acreate(
        token=token,
        discord_id=discord_id,
        discord_username=discord_username,
        expires_at=expires_at,
    )

    return f"{settings.BASE_URL}/auth/link?token={token}"


def build_link_embed(auth_url: str) -> discord.Embed:
    return discord.Embed(
        title="Link Your Discord Account",
        description=(
            f"Click the link below and log in with your team account (staff: your own account):\n\n"
            f"[Click here to link your account]({auth_url})\n\n"
            f"This link expires in {TOKEN_EXPIRY_MINUTES} minutes.\n"
            f"You will be redirected to Authentik to log in.\n"
            f"After you log in, your Discord roles follow your account within a few minutes."
        ),
        color=discord.Color.blue(),
    )


class LinkingCog(commands.Cog):
    """Commands for linking Discord accounts to teams."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="link", description="Link your Discord account to your WCComps team")
    async def link_command(self, interaction: discord.Interaction) -> None:
        """Generate a link token for user authentication."""
        user_id = interaction.user.id

        # Defer at once: the rate-limit, existing-link and token queries can exceed Discord's
        # 3s interaction window under load, which raised 10062 Unknown interaction during the competition.
        await interaction.response.defer(ephemeral=True)

        is_allowed, attempt_count = await check_rate_limit(user_id)
        if not is_allowed:
            await interaction.followup.send(
                f"Rate limit exceeded. You have made {attempt_count} link attempts in the last hour. "
                f"Please wait before trying again (limit: {RATE_LIMIT_MAX} per hour).",
                ephemeral=True,
            )
            return

        await LinkRateLimit.objects.acreate(discord_id=user_id)

        link_check = await check_existing_link(user_id)
        if not link_check.can_link:
            await interaction.followup.send(link_check.error_message, ephemeral=True)
            return

        auth_url = await create_link_token(user_id, str(interaction.user))
        embed = build_link_embed(auth_url)

        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"Generated link token for {interaction.user} ({user_id})")

    async def send_link_dm(self, user: discord.User | discord.Member) -> bool:
        """Send link instructions via DM. Returns True if sent successfully."""
        user_id = user.id

        is_allowed, _ = await check_rate_limit(user_id)
        if not is_allowed:
            logger.info(f"Rate limit exceeded for {user_id}, skipping DM")
            return False

        await LinkRateLimit.objects.acreate(discord_id=user_id)

        link_check = await check_existing_link(user_id)
        if not link_check.can_link:
            try:
                await user.send(link_check.error_message)
            except discord.Forbidden:
                logger.warning(f"Cannot DM {user_id} - DMs disabled")
            return False

        auth_url = await create_link_token(user_id, str(user))
        embed = build_link_embed(auth_url)

        try:
            await user.send(embed=embed)
            logger.info(f"Sent link DM to {user} ({user_id})")
            return True
        except discord.Forbidden:
            logger.warning(f"Cannot DM {user_id} - DMs disabled")
            return False


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LinkingCog(bot))
