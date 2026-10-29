"""Integration tests for command registration.

These tests verify that commands are properly registered in Discord's command tree.
They run in a specific order and share a single bot instance to mirror production behavior.
"""

import importlib
import inspect
import pkgutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import discord
import pytest
import pytest_asyncio
from discord.ext import commands

import bot.cogs


def discover_cogs() -> list[str]:
    """Dynamically discover all cog modules in bot/cogs/."""
    cogs_path = Path(bot.cogs.__file__).parent
    discovered = []

    for _, name, is_pkg in pkgutil.iter_modules([str(cogs_path)]):
        if not is_pkg:
            discovered.append(f"bot.cogs.{name}")

    return sorted(discovered)


def extract_commands_from_cog(cog_name: str) -> dict[str, Any]:
    """Extract command information from a cog module."""
    module = importlib.import_module(cog_name)

    # Find the cog class
    cog_class = None
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, commands.Cog) and obj != commands.Cog:
            cog_class = obj
            break

    if not cog_class:
        return {"cog_class": None, "commands": [], "groups": []}

    # Extract commands and groups
    extracted_commands = []
    extracted_groups = []

    for name, member in inspect.getmembers(cog_class):
        if hasattr(member, "__discord_app_commands_group__"):
            extracted_groups.append(name)
        elif hasattr(member, "__discord_app_commands_is_command__"):
            extracted_commands.append(name)

    return {
        "cog_class": cog_class.__name__,
        "commands": extracted_commands,
        "groups": extracted_groups,
    }


@asynccontextmanager
async def bot_with_all_cogs() -> AsyncIterator[commands.Bot]:
    """A bot with every cog loaded as an extension, as main.py loads them.

    Entered like production's login, so the cogs' loops wait for a ready that never comes instead of
    running; closing unloads the cogs, which cancels them. Unloading also deletes each cog's module from
    sys.modules, so they are put back: a later test on this worker patching "bot.cogs.x.name" would
    otherwise patch a fresh copy while the cog classes it imported keep the old module's globals.
    """
    modules = {name: module for name, module in sys.modules.items() if name.startswith("bot.")}
    async with commands.Bot(command_prefix="!", intents=discord.Intents.default()) as bot:
        for cog_module in discover_cogs():
            await bot.load_extension(cog_module)
        yield bot
    sys.modules.update(modules)


async def test_loading_the_cogs_leaves_their_modules_patchable() -> None:
    """A loaded-then-closed bot must not swap the cog modules later tests patch by path."""
    from unittest.mock import MagicMock, patch

    from bot.cogs.ticketing import TicketingCog

    async with bot_with_all_cogs():
        pass

    with patch("bot.cogs.ticketing.ticket_infrastructure", return_value=([], {}, [])) as infrastructure:
        await TicketingCog.hostname_autocomplete(MagicMock(), MagicMock(), "")

    infrastructure.assert_called_once()


class TestCommandRegistration:
    """Test that all commands are properly registered in the command tree.

    Note: Each test gets its own bot instance to avoid event loop conflicts
    when running in parallel with xdist.
    """

    @pytest_asyncio.fixture
    async def bot_with_cogs(self):
        """A bot with every cog loaded, one per test."""
        async with bot_with_all_cogs() as bot:
            yield bot

    @pytest.fixture
    def expected_cogs(self) -> dict[str, dict[str, Any]]:
        """Extract expected commands from discovered cogs."""
        cog_modules = discover_cogs()
        return {cog: extract_commands_from_cog(cog) for cog in cog_modules}

    async def test_01_all_cogs_load_without_errors(self, bot_with_cogs, expected_cogs) -> None:
        """Test that all cogs can be loaded without errors."""
        bot = bot_with_cogs

        # Verify all discovered cogs are loaded
        for cog_module, cog_info in expected_cogs.items():
            if cog_info["cog_class"]:
                assert cog_info["cog_class"] in bot.cogs, f"Cog {cog_info['cog_class']} from {cog_module} not loaded"

    async def test_02_admin_group_registered(self, bot_with_cogs) -> None:
        """Test that the admin command groups are registered."""
        bot = bot_with_cogs

        # Check that separate admin groups exist (admin, teams, tickets, competition)
        commands_list = bot.tree.get_commands()
        admin_group = next((cmd for cmd in commands_list if cmd.name == "admin"), None)
        teams_group = next((cmd for cmd in commands_list if cmd.name == "teams"), None)
        tickets_group = next((cmd for cmd in commands_list if cmd.name == "tickets"), None)
        competition_group = next((cmd for cmd in commands_list if cmd.name == "competition"), None)

        assert admin_group is not None, "Admin group not found in command tree"
        assert teams_group is not None, "Teams group not found in command tree"
        assert tickets_group is not None, "Tickets group not found in command tree"
        assert competition_group is not None, "Competition group not found in command tree"

        assert isinstance(admin_group, discord.app_commands.Group), "Admin command is not a group"
        assert isinstance(teams_group, discord.app_commands.Group), "Teams command is not a group"
        assert isinstance(tickets_group, discord.app_commands.Group), "Tickets command is not a group"
        assert isinstance(competition_group, discord.app_commands.Group), "Competition command is not a group"

    async def test_03_admin_group_has_commands(self, bot_with_cogs) -> None:
        """Test that admin groups have commands registered."""
        bot = bot_with_cogs

        commands_list = bot.tree.get_commands()
        admin_group = next((cmd for cmd in commands_list if cmd.name == "admin"), None)
        teams_group = next((cmd for cmd in commands_list if cmd.name == "teams"), None)
        tickets_group = next((cmd for cmd in commands_list if cmd.name == "tickets"), None)
        competition_group = next((cmd for cmd in commands_list if cmd.name == "competition"), None)

        assert admin_group is not None
        assert teams_group is not None
        assert tickets_group is not None
        assert competition_group is not None

        # Verify each group has commands
        assert len(admin_group.commands) > 0, "Admin group exists but has no commands"
        assert len(teams_group.commands) > 0, "Teams group exists but has no commands"
        assert len(tickets_group.commands) > 0, "Tickets group exists but has no commands"
        assert len(competition_group.commands) > 0, "Competition group exists but has no commands"

    async def test_04_admin_subgroups_exist(self, bot_with_cogs) -> None:
        """Test that command groups are flat (no nested subgroups needed)."""
        bot = bot_with_cogs

        # With the new structure, we have flat command groups (admin, teams, tickets, competition)
        # No subgroups are expected, so this test just verifies the groups exist at top level
        commands_list = bot.tree.get_commands()
        group_names = [cmd.name for cmd in commands_list if isinstance(cmd, discord.app_commands.Group)]

        # Verify all four expected groups are present
        assert "admin" in group_names, "Admin group not found at top level"
        assert "teams" in group_names, "Teams group not found at top level"
        assert "tickets" in group_names, "Tickets group not found at top level"
        assert "competition" in group_names, "Competition group not found at top level"

    async def test_05_user_commands_registered(self, bot_with_cogs) -> None:
        """Test that user-facing commands are registered."""
        bot = bot_with_cogs

        commands_list = bot.tree.get_commands()

        # Verify we have top-level commands beyond admin
        top_level_commands = [cmd for cmd in commands_list if not isinstance(cmd, discord.app_commands.Group)]

        assert len(top_level_commands) > 0, "No top-level user commands found (only groups exist)"

    async def test_06_no_duplicate_command_groups(self, bot_with_cogs) -> None:
        """Test that there are no duplicate command groups after loading all cogs."""
        bot = bot_with_cogs

        commands_list = bot.tree.get_commands()
        command_names = [cmd.name for cmd in commands_list]

        # Check for duplicates
        seen = set()
        duplicates = set()
        for name in command_names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)

        assert len(duplicates) == 0, (
            f"Found duplicate command groups: {duplicates}. Multiple cogs may be creating duplicate groups."
        )

    async def test_07_command_descriptions_present(self, bot_with_cogs) -> None:
        """Test that all commands have descriptions."""
        bot = bot_with_cogs

        commands_list = bot.tree.get_commands()

        for cmd in commands_list:
            assert cmd.description, f"Command {cmd.name} has no description"

            # Check subcommands if it's a group
            if isinstance(cmd, discord.app_commands.Group):
                for subcmd in cmd.commands:
                    assert subcmd.description, f"Subcommand {cmd.name}/{subcmd.name} has no description"

                    # Check nested subgroups
                    if isinstance(subcmd, discord.app_commands.Group):
                        for nested_cmd in subcmd.commands:
                            assert nested_cmd.description, (
                                f"Nested command {cmd.name}/{subcmd.name}/{nested_cmd.name} has no description"
                            )

    async def test_08_command_tree_walkable(self, bot_with_cogs) -> None:
        """Test that the command tree can be walked without errors (validates sync would work)."""
        bot = bot_with_cogs

        # Get all commands - this internally validates the tree structure
        commands_list = bot.tree.get_commands()

        # Verify we have commands
        assert len(commands_list) > 0, "No commands found in tree"

        # The tree.walk_commands() method is what sync() uses internally
        # If we can iterate through it without errors, sync would likely work
        all_commands = list(bot.tree.walk_commands())
        assert len(all_commands) > 0, "No commands found when walking tree"

        # Verify we have commands (without hardcoding exact count)
        assert len(all_commands) >= 10, f"Expected at least 10 commands, found {len(all_commands)}"
