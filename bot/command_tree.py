"""The bot's command tree: what every slash command gets before and after its own handler."""

import contextlib
import logging

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
