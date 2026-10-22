import hashlib
import logging
import os
import sys

import discord
import django
from discord.ext import commands

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")
django.setup()

from django.conf import settings

from bot.command_tree import PortalCommandTree
from bot.competition_timer import CompetitionTimer
from bot.discord_queue import DiscordQueueProcessor
from bot.unified_dashboard import UnifiedDashboard
from bot.utils import ALLOWED_MENTIONS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
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

    def _get_command_hash(self) -> str:
        """Hash cog and command-parameter source files to detect command changes."""
        from pathlib import Path

        root = Path(__file__).parent
        cogs_dir = root / "bot" / "cogs"
        tracked_files = [
            *sorted(cogs_dir.glob("*.py")),
            root / "bot" / "utils.py",
            root / "web" / "ticketing" / "models.py",
            root / "web" / "scoring" / "models.py",
        ]
        hasher = hashlib.sha256()
        for path in tracked_files:
            if path.exists():
                hasher.update(path.read_bytes())
        return hasher.hexdigest()[:16]

    async def _should_sync_commands(self) -> bool:
        from asgiref.sync import sync_to_async

        from core.models import BotState

        current_hash = self._get_command_hash()

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
        await self.load_extension("bot.cogs.quotient_sync")
        await self.load_extension("bot.cogs.authentik_groups")
        await self.add_cog(CompetitionTimer(self))

        logger.info("Cogs loaded")

        from bot.ticket_dashboard import TicketActionView

        self.add_view(TicketActionView(ticket_id=0))
        logger.info("Registered persistent ticket action view")

        commands_list = self.tree.get_commands()
        logger.info(f"Registered {len(commands_list)} top-level commands:")
        for cmd in commands_list:
            if isinstance(cmd, discord.app_commands.Group):
                logger.info(f"  - {cmd.name} (Group with {len(cmd.commands)} subcommands)")
            else:
                logger.info(f"  - {cmd.name} (Command)")

        # Competition guild gets all commands
        competition_guild_id = settings.COMPETITION_GUILD_ID
        # Volunteer guild gets only the /link command so staff can link their accounts there
        volunteer_guild_id = settings.VOLUNTEER_GUILD_ID

        if not await self._should_sync_commands():
            return

        synced = True

        if competition_guild_id:
            guild = discord.Object(id=competition_guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                logger.info(f"Command tree synced to competition guild ({competition_guild_id})")
            except discord.HTTPException as e:
                synced = False
                logger.warning(f"Guild sync failed: {e}")

        # Clear global commands to avoid duplicates with guild commands
        self.tree.clear_commands(guild=None)
        try:
            await self.tree.sync()
            logger.info("Cleared global commands")
        except discord.HTTPException as e:
            synced = False
            logger.warning(f"Failed to clear global commands: {e}")

        if volunteer_guild_id and volunteer_guild_id != competition_guild_id:
            volunteer_guild = discord.Object(id=volunteer_guild_id)
            self.tree.clear_commands(guild=volunteer_guild)
            link_command = self.tree.get_command("link")
            if link_command:
                self.tree.add_command(link_command, guild=volunteer_guild)
                try:
                    await self.tree.sync(guild=volunteer_guild)
                    logger.info(f"Synced /link command to volunteer guild ({volunteer_guild_id})")
                except discord.HTTPException as e:
                    synced = False
                    logger.warning(f"Volunteer guild sync failed: {e}")

        # Only a complete sync is recorded, so a failed one is retried on the next start.
        if synced:
            from core.models import BotState

            await BotState.objects.aupdate_or_create(key="command_hash", defaults={"value": self._get_command_hash()})

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
            from bot.utils import report_missing_discord_settings

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


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("DISCORD_BOT_TOKEN environment variable not set")
        sys.exit(1)

    bot = PortalBot()

    try:
        bot.run(token)
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
