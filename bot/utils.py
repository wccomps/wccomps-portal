"""Utility functions for the bot."""

import io
import logging
from typing import Final, Literal

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from django.conf import settings
from django.db import close_old_connections

from core.utils import missing_discord_settings
from team.models import MAX_TEAMS, Team

logger = logging.getLogger(__name__)


THREAD_AUTO_ARCHIVE_MINUTES: Final[Literal[10080]] = 10080  # 7 days
DISCORD_EMBED_FIELD_CHAR_LIMIT = 1024
DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT = 4096
DISCORD_EMBED_TITLE_CHAR_LIMIT = 256
DISCORD_MESSAGE_CHAR_LIMIT = 2000
DISCORD_EMBED_FIELD_NAME_CHAR_LIMIT = 256
DISCORD_EMBED_TOTAL_CHAR_LIMIT = 6000
DISCORD_EMBED_FIELD_COUNT_LIMIT = 25

# What a message may ping unless its send names more: nothing. Messages repeat user text (ticket descriptions,
# broadcasts, team names), so @everyone, @here or a mention in it must not reach anyone.
ALLOWED_MENTIONS = discord.AllowedMentions.none()


def pinging(
    *, users: list[discord.abc.Snowflake] | None = None, roles: list[discord.abc.Snowflake] | None = None
) -> discord.AllowedMentions:
    """Allowed mentions for a message meant to ping these users or roles, and no one else its text names."""
    return discord.AllowedMentions(everyone=False, users=users or False, roles=roles or False, replied_user=False)


def fit(text: str, limit: int) -> str:
    """text cut to Discord's limit, marked with an ellipsis; Discord rejects the whole message otherwise."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


DISCORD_CHOICE_CHAR_LIMIT = 100
DISCORD_CHOICE_COUNT_LIMIT = 25


def choices(options: list[tuple[str, str]]) -> list[app_commands.Choice[str]]:
    """Autocomplete choices from (name, value) pairs as Discord takes them: at most 25, each name cut to 100
    characters. A value past 100 characters is left out: Discord couldn't send it back."""
    return [
        app_commands.Choice(name=fit(name.strip() or value, DISCORD_CHOICE_CHAR_LIMIT), value=value)
        for name, value in options
        if 0 < len(value) <= DISCORD_CHOICE_CHAR_LIMIT
    ][:DISCORD_CHOICE_COUNT_LIMIT]


def fit_lines(lines: list[str], limit: int) -> str:
    """As many whole lines as fit in limit characters, and how many more there are."""
    shown: list[str] = []
    for line in lines:
        more = len(lines) - len(shown) - 1
        # Leave room for the "... and N more" line unless this is the last line
        if len("\n".join([*shown, line])) + (len(f"\n... and {more} more") if more else 0) > limit:
            break
        shown.append(line)
    rest = len(lines) - len(shown)
    return "\n".join([*shown, f"... and {rest} more"] if rest else shown)


def add_field(embed: discord.Embed, name: str, value: str, *, inline: bool = False, room: int = 0) -> bool:
    """Add a field cut to Discord's limits for one, if the embed has room for it (25 fields, 6000 characters in
    all, less `room` kept for what is added after); a blank value (which Discord refuses) adds nothing. Returns
    whether it was added."""
    name = fit(name.strip() or "Unnamed", DISCORD_EMBED_FIELD_NAME_CHAR_LIMIT)
    value = fit(value, DISCORD_EMBED_FIELD_CHAR_LIMIT)
    if not value.strip():
        return False
    if len(embed.fields) >= DISCORD_EMBED_FIELD_COUNT_LIMIT or len(embed) + len(name) + len(value) > (
        DISCORD_EMBED_TOTAL_CHAR_LIMIT - room
    ):
        return False
    embed.add_field(name=name, value=value, inline=inline)
    return True


def split_message(text: str, limit: int = DISCORD_MESSAGE_CHAR_LIMIT) -> list[str]:
    """text in Discord-sized messages, broken at line ends where possible, so nothing is lost."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*chunks, text] if text or not chunks else chunks


def announcement(sender: str, message: str) -> str:
    """A broadcast as a team's channel shows it."""
    return f"**Announcement from {sender}:**\n\n{message}"


async def send_parts(
    channel: discord.abc.Messageable, text: str, *, allowed_mentions: discord.AllowedMentions = ALLOWED_MENTIONS
) -> None:
    """Send text in as many messages as it takes (split_message), so none is past Discord's 2000 characters. Only
    the first may ping what allowed_mentions allows: the rest is the sender's text."""
    for n, part in enumerate(split_message(text)):
        await channel.send(part, allowed_mentions=allowed_mentions if n == 0 else ALLOWED_MENTIONS)


async def log_to_ops_channel(bot: discord.Client, message: str, embed: discord.Embed | None = None) -> None:
    try:
        channel_id = settings.DISCORD_LOG_CHANNEL_ID
        if not channel_id:
            logger.warning("DISCORD_LOG_CHANNEL_ID not configured")
            return

        channel = bot.get_channel(channel_id)
        if not channel:
            logger.error(f"Operations channel {channel_id} not found")
            return

        message = fit(message, DISCORD_MESSAGE_CHAR_LIMIT)
        # Type guard: only TextChannel and Thread have send()
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            if embed:
                await channel.send(message, embed=embed)
            else:
                await channel.send(message)
            logger.info(f"Logged to ops channel: {message}")
        else:
            logger.error(f"Channel {channel_id} is not a text channel or thread")
    except Exception as e:
        logger.exception(f"Failed to log to ops channel: {e}")


async def recycle_db_connection() -> None:
    """Drop the bot's DB connection if it is broken or past CONN_MAX_AGE; the next query reconnects.

    Django only does this on request_started/request_finished, and the bot serves no requests,
    so after a Postgres restart its single connection stayed dead and every loop failed forever.
    Must run via sync_to_async's default (thread_sensitive=True) executor: that one thread owns
    the connection all bot ORM calls use.
    """
    await sync_to_async(close_old_connections)()


async def report_missing_discord_settings(bot: discord.Client) -> None:
    """Log and post to the ops channel any required Discord IDs that are unset.

    Unset IDs used to fail silently (roles/channels "not found"), so this runs at startup.
    """
    missing = missing_discord_settings()
    if not missing:
        return
    message = (
        f"Discord settings missing from the environment (set to 0): {', '.join(missing)}. "
        "Role sync, /link roles, team channel access and announcements that depend on them will not work."
    )
    logger.error(message)
    await log_to_ops_channel(bot, message)


async def get_team_or_respond(
    interaction: discord.Interaction, team_number: int, validate_range: bool = True
) -> Team | None:
    """Get a team by number, or send the user an error and return None."""
    from team.models import Team

    if validate_range and (team_number < 1 or team_number > MAX_TEAMS):
        await interaction.response.send_message(f"Team number must be between 1 and {MAX_TEAMS}", ephemeral=True)
        return None

    team = await Team.objects.filter(team_number=team_number).afirst()
    if not team:
        await interaction.response.send_message(f"Team {team_number} not found", ephemeral=True)
        return None
    return team


def team_chat_channel(guild: discord.Guild, team: Team) -> discord.TextChannel | None:
    """The text channel with "chat" in its name in the team's category, which may predate the bot."""
    category = guild.get_channel(team.discord_category_id) if team.discord_category_id else None
    if not isinstance(category, discord.CategoryChannel):
        return None
    return next((c for c in category.channels if isinstance(c, discord.TextChannel) and "chat" in c.name.lower()), None)


@sync_to_async
def get_team_member_discord_ids(team: Team) -> list[int]:
    return list(team.members.filter(is_active=True).values_list("discord_id", flat=True))


class ConfirmView(discord.ui.View):
    """Reusable confirmation dialog with confirm/cancel buttons."""

    def __init__(
        self,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        timeout: float = 60,
    ) -> None:
        super().__init__(timeout=timeout)
        self.confirmed: bool | None = None
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self._setup_buttons()

    def _setup_buttons(self) -> None:
        confirm_btn = ConfirmButton(label=self._confirm_label, style=discord.ButtonStyle.danger)
        cancel_btn = CancelButton(label=self._cancel_label, style=discord.ButtonStyle.secondary)
        self.add_item(confirm_btn)
        self.add_item(cancel_btn)


class ConfirmButton(discord.ui.Button["ConfirmView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view:
            self.view.confirmed = True
            self.view.stop()
        await interaction.response.defer()


class CancelButton(discord.ui.Button["ConfirmView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view:
            self.view.confirmed = False
            self.view.stop()
        await interaction.response.defer()


async def send_lines(
    interaction: discord.Interaction, header: str, lines: list[str], *, title: str, filename: str
) -> None:
    """Send header plus as many lines as fit in one message; attach the full list if they don't all fit."""
    text = f"{header}\n\n**{title}:**\n" if lines else header
    shown = 0
    for line in lines:
        # Leave room for the "... and N more" line.
        if len(text) + len(line) + 1 > DISCORD_MESSAGE_CHAR_LIMIT - 60:
            break
        text += line + "\n"
        shown += 1
    if shown == len(lines):
        await interaction.followup.send(text, ephemeral=True)
        return
    text += f"... and {len(lines) - shown} more (full list attached)"
    full = discord.File(io.BytesIO("\n".join(lines).encode()), filename=filename)
    await interaction.followup.send(text, file=full, ephemeral=True)
