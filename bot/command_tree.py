"""The bot's command tree: what every slash command gets before and after its own handler."""

import contextlib
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Protocol

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

NUL_REFUSAL = "Your text has a NUL character (\\0) in it, which can't be stored. Remove it and try again."
FAILED = "Something went wrong running that command. Try again, or ask an admin if it keeps failing."


def holds_nul(value: object) -> bool:
    """Whether a NUL character is anywhere in the interaction data (an option's value, at any depth)."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(holds_nul(v) for v in value.values())
    if isinstance(value, list):
        return any(holds_nul(v) for v in value)
    return False


class PortalCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Refuse an option holding a NUL: PostgreSQL text can't hold one, so any lookup or save with it would fail
        (the web refuses such a request the same way, core.middleware)."""
        if not holds_nul(interaction.data):
            return True
        if interaction.type is discord.InteractionType.autocomplete:
            await interaction.response.autocomplete([])
        else:
            await interaction.response.send_message(NUL_REFUSAL, ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Answer a command that failed, so no one is left with "The application did not respond". A failed check
        has answered already; an option the bot couldn't read (a transformer's error) says why."""
        if isinstance(error, app_commands.CheckFailure):
            return
        if not isinstance(error, app_commands.CommandInvokeError):
            message = str(error)
        else:
            logger.error(f"Command {interaction.command and interaction.command.name} failed", exc_info=error)
            message = FAILED
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)


class SyncedCommand(Protocol):
    def to_dict(self, tree: app_commands.CommandTree) -> dict[str, object]: ...


class _Commands[C](Protocol):
    def get_commands(self) -> list[C]: ...
    def get_command(self, command: str) -> C | None: ...


def command_plan[C](
    tree: _Commands[C], competition_guild_id: int, volunteer_guild_id: int
) -> dict[int | None, list[C]]:
    """Each guild's commands (None: the global ones), read before a sync changes the tree: all of them to the
    competition guild, /link alone to the volunteer guild, none global (each would show twice)."""
    plan: dict[int | None, list[C]] = {None: []}
    if competition_guild_id:
        plan[competition_guild_id] = list(tree.get_commands())
    if volunteer_guild_id and volunteer_guild_id != competition_guild_id:
        link = tree.get_command("link")
        plan[volunteer_guild_id] = [link] if link else []
    return plan


def command_hash(tree: app_commands.CommandTree, plan: Mapping[int | None, Sequence[SyncedCommand]]) -> str:
    """A fingerprint of what syncing the plan sends Discord: each guild's commands as Discord receives them."""
    payload = {
        str(guild): sorted((command.to_dict(tree) for command in commands), key=lambda c: (c["type"], c["name"]))
        for guild, commands in plan.items()
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
