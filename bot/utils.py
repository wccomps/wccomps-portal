import asyncio
import functools
import io
import logging
from collections.abc import Awaitable, Callable, Collection, Generator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Final, Literal, TypedDict

import discord
from asgiref.sync import sync_to_async
from discord import app_commands
from django.conf import settings
from django.db import close_old_connections, connection

from core.utils import missing_discord_settings
from team.models import MAX_TEAMS, Team

logger = logging.getLogger(__name__)


THREAD_AUTO_ARCHIVE_MINUTES: Final[Literal[10080]] = 10080
DISCORD_EMBED_FIELD_CHAR_LIMIT = 1024
DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT = 4096
DISCORD_EMBED_TITLE_CHAR_LIMIT = 256
DISCORD_MESSAGE_CHAR_LIMIT = 2000
DISCORD_EMBED_FIELD_NAME_CHAR_LIMIT = 256
DISCORD_EMBED_TOTAL_CHAR_LIMIT = 6000
DISCORD_EMBED_FIELD_COUNT_LIMIT = 25
DISCORD_THREAD_NAME_CHAR_LIMIT = 100

# What a message may ping unless its send names more: nothing. Messages repeat user text (ticket descriptions,
# broadcasts, team names), so @everyone, @here or a mention in it must not reach anyone.
ALLOWED_MENTIONS = discord.AllowedMentions.none()


def pinging(
    *, users: list[discord.abc.Snowflake] | None = None, roles: list[discord.abc.Snowflake] | None = None
) -> discord.AllowedMentions:
    """Allowed mentions for a message meant to ping these users or roles, and no one else its text names."""
    return discord.AllowedMentions(everyone=False, users=users or False, roles=roles or False, replied_user=False)


def fit(text: str, limit: int) -> str:
    """text cut to at most limit characters (with "…", unlike core.utils.shorten, never past limit)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


DISCORD_CHOICE_CHAR_LIMIT = 100
DISCORD_CHOICE_COUNT_LIMIT = 25
DISCORD_SELECT_OPTION_LIMIT = 25


def choices(options: list[tuple[str, str]]) -> list[app_commands.Choice[str]]:
    """Autocomplete choices from (name, value) pairs as Discord takes them: at most 25, each name cut to 100
    characters, and values past 100 characters omitted (Discord refuses a choice value over 100 characters)."""
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
    """Add a field fit to Discord's limits if the embed has room (25 fields, 6000 - room chars) and value is
    non-blank; returns whether added."""
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


class ProgressState(TypedDict, total=False):
    done: int  # steps done, for a task that counts them
    # A broadcast's: recipients reached, refused for good (with why), counts, and parts sent to each it failed on
    reached: list[int]
    refused: dict[str, str]
    sent: int
    queued: int
    parts: dict[str, int]


@dataclass
class TaskProgress:
    """What a queued task already did, saved with the task so a retry does only the rest."""

    state: ProgressState = field(default_factory=ProgressState)
    save: Callable[[ProgressState], Awaitable[None]] | None = None

    @property
    def done(self) -> int:
        return self.state.get("done", 0)

    async def advance(self) -> None:
        self.state["done"] = self.done + 1
        await self.saved()

    async def saved(self) -> None:
        if self.save:
            await self.save(self.state)


async def send_parts(
    channel: discord.abc.Messageable,
    text: str,
    *,
    allowed_mentions: discord.AllowedMentions = ALLOWED_MENTIONS,
    progress: TaskProgress | None = None,
) -> None:
    """Send text in as many messages as it takes (split_message), so none is past Discord's 2000 characters. Only
    the first may ping what allowed_mentions allows: the rest is the sender's text. With progress, parts an earlier
    attempt sent are skipped."""
    progress = progress or TaskProgress()
    for n, part in enumerate(split_message(text)):
        if n < progress.done:
            continue
        await channel.send(part, allowed_mentions=allowed_mentions if n == 0 else ALLOWED_MENTIONS)
        await progress.advance()


async def send_announcement(
    guild: discord.Guild, channel: discord.abc.Messageable, message: str, progress: TaskProgress | None = None
) -> None:
    """Send a broadcast to the announcements channel, pinging only the blue team role."""
    blueteam_role = guild.get_role(settings.BLUETEAM_ROLE_ID)
    role_mention = blueteam_role.mention if blueteam_role else "@Blueteam"
    await send_parts(
        channel,
        f"{role_mention}\n\n{message}",
        allowed_mentions=pinging(roles=[blueteam_role] if blueteam_role else []),
        progress=progress,
    )


async def log_to_ops_channel(bot: discord.Client, message: str) -> bool:
    """Post to the ops channel; whether it was posted. Never raises: a failure is logged."""
    try:
        channel_id = settings.DISCORD_LOG_CHANNEL_ID
        if not channel_id:
            logger.warning("DISCORD_LOG_CHANNEL_ID not configured")
            return False

        channel = bot.get_channel(channel_id)
        if not channel:
            logger.error(f"Operations channel {channel_id} not found")
            return False

        message = fit(message, DISCORD_MESSAGE_CHAR_LIMIT)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.error(f"Channel {channel_id} is not a text channel or thread")
            return False
        await channel.send(message)
        logger.info(f"Logged to ops channel: {message}")
        return True
    except Exception as e:
        logger.exception(f"Failed to log to ops channel: {e}")
        return False


async def recycle_db_connection() -> None:
    """Drop the bot's DB connection if it is broken or past CONN_MAX_AGE; the next query reconnects.

    Django only does this on request_started/request_finished, and the bot serves no requests,
    so after a Postgres restart its single connection stayed dead and every loop failed forever.
    Must run via sync_to_async's default (thread_sensitive=True) executor: that one thread owns
    the connection the bot's ordinary ORM calls use.
    """
    await sync_to_async(close_old_connections)()


async def run_blocking[**P, R](func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run slow sync work (Authentik, Quotient) on a pool thread, not the one thread every other
    sync_to_async call in the bot queues on, so interactions and loops keep answering meanwhile.
    """
    return await _hop(functools.partial(func, *args, **kwargs), None, release=True)


async def _hop[T](step: Callable[[], T], thread: ThreadPoolExecutor | None, *, release: bool) -> T:
    """Run step off the event loop, on thread or a pool thread when None. release closes that thread's connection
    after, which recycle_db_connection never reaches."""

    def run() -> T:
        try:
            return step()
        finally:
            if release:
                connection.close()

    if thread is None:
        return await sync_to_async(run, thread_sensitive=False)()
    return await asyncio.get_running_loop().run_in_executor(thread, run)


class BlockingSteps[Y, R]:
    """Step a sync generator on a pool thread of its own, an await per item, so a lock it takes stays on one
    connection. Use it under contextlib.aclosing, which closes the generator on that thread; .result is its return."""

    def __init__(self, steps: Generator[Y, None, R]) -> None:
        self._steps = steps
        self._returned: tuple[R] | None = None
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blocking-steps")

    def __aiter__(self) -> BlockingSteps[Y, R]:
        return self

    async def __anext__(self) -> Y:
        for item in await _hop(self._advance, self._thread, release=False):
            return item
        raise StopAsyncIteration

    async def aclose(self) -> None:
        try:
            await _hop(self._steps.close, self._thread, release=True)
        finally:
            self._thread.shutdown(wait=False)

    def _advance(self) -> list[Y]:
        try:
            return [next(self._steps)]
        except StopIteration as finished:
            self._returned = (finished.value,)
            return []

    @property
    def result(self) -> R:
        if self._returned is None:
            raise RuntimeError("The generator has not finished")
        return self._returned[0]


async def report_missing_discord_settings(bot: discord.Client) -> None:
    """Log and post to the ops channel any required Discord IDs that are unset."""
    missing = missing_discord_settings()
    if not missing:
        return
    message = (
        f"Discord settings missing from the environment (set to 0): {', '.join(missing)}. "
        "Role sync, /link roles, team channel access and announcements that depend on them will not work."
    )
    logger.error(message)
    await log_to_ops_channel(bot, message)


async def find_team(team_number: int) -> Team | str:
    """The team with this number, or why there is none."""
    if team_number < 1 or team_number > MAX_TEAMS:
        return f"Team number must be between 1 and {MAX_TEAMS}"
    team = await Team.objects.filter(team_number=team_number).afirst()
    return team or f"Team {team_number} not found"


def team_role_name(team_number: int) -> str:
    return f"Team {team_number:02d}"


def team_category_name(team_number: int) -> str:
    return f"team {team_number:02d}"


def find_team_role(guild: discord.Guild, team: Team, gone: Collection[int] = ()) -> discord.Role | None:
    """The team's role by its recorded ID, else by name; never one in gone (just deleted, though the cache may still
    hold it)."""
    role = guild.get_role(team.discord_role_id) if team.discord_role_id else None
    if role and role.id not in gone:
        return role
    names = (team_role_name(team.team_number), f"Team {team.team_number}")  # older roles weren't zero padded
    return _named(guild.roles, names, gone)


def find_team_category(guild: discord.Guild, team: Team, gone: Collection[int] = ()) -> discord.CategoryChannel | None:
    """The team's category by its recorded ID, else by name (one the bot lost track of, or that predates it); never
    one in gone."""
    category = guild.get_channel(team.discord_category_id) if team.discord_category_id else None
    if isinstance(category, discord.CategoryChannel) and category.id not in gone:
        return category
    return _named(guild.categories, (team_category_name(team.team_number), f"team {team.team_number}"), gone)


def _named[T: discord.Role | discord.CategoryChannel](
    items: Sequence[T], names: tuple[str, ...], gone: Collection[int]
) -> T | None:
    return next((item for name in names for item in items if item.name == name and item.id not in gone), None)


def team_chat_channel(guild: discord.Guild, team: Team) -> discord.TextChannel | None:
    """The text channel with "chat" in its name in the team's category, which may predate the bot; none while the
    team is marked channels_need_clearing."""
    if team.channels_need_clearing:
        return None
    category = find_team_category(guild, team)
    return category_chat_channel(category) if category else None


def category_chat_channel(category: discord.CategoryChannel) -> discord.TextChannel | None:
    return next((c for c in category.channels if isinstance(c, discord.TextChannel) and "chat" in c.name.lower()), None)


class ConfirmView(discord.ui.View):
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
