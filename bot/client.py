"""The Discord client the chat process runs: its cogs, its slash-command sync, the Discord queue and the dashboard."""

import logging
import os

import discord
from asgiref.sync import sync_to_async
from discord.ext import commands
from django.conf import settings

from bot.command_tree import PortalCommandTree, command_hash, command_plan
from bot.discord_queue import DiscordQueueProcessor
from bot.ticket_dashboard import TicketActionView
from bot.unified_dashboard import UnifiedDashboard
from bot.utils import ALLOWED_MENTIONS, report_missing_discord_settings
from core.models import BotState

logger = logging.getLogger(__name__)


class PortalBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix="!", intents=intents, allowed_mentions=ALLOWED_MENTIONS, tree_cls=PortalCommandTree
        )

        self.queue_processor: DiscordQueueProcessor | None = None
        self.unified_dashboard: UnifiedDashboard | None = None

    async def _should_sync_commands(self, current_hash: str) -> bool:
        if os.environ.get("SYNC_COMMANDS", "").lower() in ("true", "1", "yes"):
            logger.info(f"SYNC_COMMANDS=true, forcing sync (hash: {current_hash})")
            return True

        try:
            stored = await sync_to_async(BotState.objects.get)(key="command_hash")
            if stored.value == current_hash:
                logger.info(f"Commands unchanged (hash: {current_hash}), skipping sync")
                return False
            logger.info(f"Commands changed ({stored.value} -> {current_hash}), will sync")
        except BotState.DoesNotExist:
            logger.info(f"No stored command hash, will sync (hash: {current_hash})")
        return True

    async def setup_hook(self) -> None:
        """Load cogs and sync slash commands; discord.py runs this during login, before on_ready."""
        logger.info("Loading cogs...")

        await self.load_extension("bot.cogs.linking")
        await self.load_extension("bot.cogs.ticketing")
        await self.load_extension("bot.cogs.scoring")
        await self.load_extension("bot.cogs.help_panels")
        await self.load_extension("bot.cogs.admin")
        await self.load_extension("bot.cogs.admin_teams")
        await self.load_extension("bot.cogs.admin_tickets")
        await self.load_extension("bot.cogs.admin_competition")
        await self.load_extension("bot.cogs.role_sync")

        logger.info("Cogs loaded")

        self.add_view(TicketActionView(ticket_id=0))
        logger.info("Registered persistent ticket action view")

        commands_list = self.tree.get_commands()
        logger.info(f"Registered {len(commands_list)} top-level commands:")
        for cmd in commands_list:
            if isinstance(cmd, discord.app_commands.Group):
                logger.info(f"  - {cmd.name} (Group with {len(cmd.commands)} subcommands)")
            else:
                logger.info(f"  - {cmd.name} (Command)")

        plan = command_plan(self.tree, settings.COMPETITION_GUILD_ID, settings.VOLUNTEER_GUILD_ID)
        current_hash = command_hash(self.tree, plan)
        if not await self._should_sync_commands(current_hash):
            return

        synced = True
        for guild_id, guild_commands in plan.items():
            guild = discord.Object(id=guild_id) if guild_id else None
            where = f"guild {guild_id}" if guild_id else "global"
            self.tree.clear_commands(guild=guild)
            for command in guild_commands:
                self.tree.add_command(command, guild=guild)
            try:
                await self.tree.sync(guild=guild)
                logger.info(f"Synced {len(guild_commands)} commands to {where}")
            except discord.HTTPException as e:
                synced = False
                logger.warning(f"Command sync to {where} failed: {e}")

        # Only a complete sync is recorded, so a failed one is retried on the next start.
        if synced:
            await BotState.objects.aupdate_or_create(key="command_hash", defaults={"value": current_hash})

    async def on_ready(self) -> None:
        if not self.user:
            logger.error("Bot user is None in on_ready")
            return
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")

        for guild in self.guilds:
            logger.info(f"  - {guild.name} (ID: {guild.id})")

        if not self.queue_processor:
            self.queue_processor = DiscordQueueProcessor(self)
            self.queue_processor.start()

            # First ready only (on_ready also fires on reconnects): flag unset Discord IDs
            await report_missing_discord_settings(self)

        if not self.unified_dashboard:
            self.unified_dashboard = UnifiedDashboard(self)
            self.unified_dashboard.start()

    async def close(self) -> None:
        logger.info("Shutting down bot...")
        if self.queue_processor:
            self.queue_processor.stop()
        if self.unified_dashboard:
            self.unified_dashboard.stop()
        await super().close()
