"""Slash commands are re-synced on start when the hash of the command plan differs from the last full sync's."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from core.models import BotState

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


async def _run_setup_hook(sync: AsyncMock, settings: Any) -> None:
    from main import PortalBot

    settings.COMPETITION_GUILD_ID = 1
    settings.VOLUNTEER_GUILD_ID = 0

    bot = PortalBot()
    with (
        patch.object(bot, "load_extension", new_callable=AsyncMock),
        patch.object(bot, "add_cog", new_callable=AsyncMock),
        patch.object(bot, "add_view"),
        patch.object(bot.tree, "sync", sync),
    ):
        await bot.setup_hook()


async def test_failed_sync_is_not_recorded(db: Any, settings: Any) -> None:
    await _run_setup_hook(AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom")), settings)
    assert not await BotState.objects.filter(key="command_hash").aexists()


async def test_successful_sync_is_recorded_and_skipped_next_time(db: Any, settings: Any) -> None:
    await _run_setup_hook(AsyncMock(), settings)
    assert await BotState.objects.filter(key="command_hash").aexists()

    second = AsyncMock()
    await _run_setup_hook(second, settings)
    second.assert_not_awaited()


def _tree_with(description: str, *names: str) -> discord.app_commands.CommandTree:
    from main import PortalBot

    async def callback(interaction: discord.Interaction) -> None:
        pass

    bot = PortalBot()
    for name in names or ("ping",):
        bot.tree.add_command(discord.app_commands.Command(name=name, description=description, callback=callback))
    return bot.tree


def _hash(tree: discord.app_commands.CommandTree, competition: int, volunteer: int) -> str:
    from bot.command_tree import command_hash, command_plan

    return command_hash(tree, command_plan(tree, competition, volunteer))


async def test_the_hash_is_what_the_sync_sends() -> None:
    """Any change to a registered command's name, options or description changes it, wherever its code lives;
    so does a change of guild, or of which commands a guild gets."""
    assert _hash(_tree_with("Ping"), 1, 0) == _hash(_tree_with("Ping"), 1, 0)
    assert _hash(_tree_with("Ping"), 1, 0) != _hash(_tree_with("Ping the bot"), 1, 0)
    assert _hash(_tree_with("Ping"), 1, 0) != _hash(_tree_with("Ping"), 2, 0)
    assert _hash(_tree_with("Ping", "ping", "link"), 1, 0) != _hash(_tree_with("Ping", "ping", "link"), 1, 2)


async def _setup_with(bot: Any, commands: list[Any], sync: AsyncMock) -> None:
    async def load(name: str) -> None:
        if not bot.tree.get_commands():
            for command in commands:
                bot.tree.add_command(command)

    with (
        patch.object(bot, "load_extension", side_effect=load),
        patch.object(bot, "add_cog", new_callable=AsyncMock),
        patch.object(bot, "add_view"),
        patch.object(bot.tree, "sync", sync),
    ):
        await bot.setup_hook()


async def test_the_recorded_hash_is_the_tree_the_cogs_registered(db: Any, settings: Any) -> None:
    """Recorded after the sync, which clears the global commands: the hash must be taken before it does."""
    from main import PortalBot

    settings.COMPETITION_GUILD_ID = 1
    settings.VOLUNTEER_GUILD_ID = 0
    await _setup_with(PortalBot(), _tree_with("Ping").get_commands(), AsyncMock())

    stored = await BotState.objects.aget(key="command_hash")
    assert stored.value == _hash(_tree_with("Ping"), 1, 0)


async def test_each_guild_gets_its_commands(db: Any, settings: Any) -> None:
    from main import PortalBot

    settings.COMPETITION_GUILD_ID = 1
    settings.VOLUNTEER_GUILD_ID = 2
    bot = PortalBot()
    synced: dict[int | None, list[str]] = {}

    async def record(*, guild: Any = None) -> list[Any]:
        commands = bot.tree.get_commands(guild=guild)
        synced[guild.id if guild else None] = sorted(command.name for command in commands)
        return []

    await _setup_with(bot, _tree_with("Ping", "ping", "link").get_commands(), AsyncMock(side_effect=record))

    assert synced == {1: ["link", "ping"], None: [], 2: ["link"]}
