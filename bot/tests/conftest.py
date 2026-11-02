"""Pytest fixtures for bot command testing."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from django.contrib.auth.models import User

from core.models import UserGroups
from team.models import DiscordLink, Team
from ticketing.models import TicketCategory


@pytest.fixture(autouse=True)
def refresh_user_groups() -> Iterator[MagicMock]:
    """Starting or stopping the competition refreshes groups from Authentik; keep tests off the network."""
    with patch("core.services.user_groups.refresh_user_groups") as mock:
        yield mock


@pytest_asyncio.fixture
async def box_reset_category(db: Any) -> TicketCategory:
    """Create box-reset ticket category for bot tests."""
    cat, _ = await TicketCategory.objects.aget_or_create(
        pk=2,
        defaults={
            "display_name": "Box Reset",
            "points": 60,
            "required_fields": ["hostname", "ip_address"],
            "optional_fields": ["service_name"],
            "sort_order": 1,
        },
    )
    return cat


@pytest_asyncio.fixture
async def scoring_check_category(db: Any) -> TicketCategory:
    """Create scoring-service-check ticket category for bot tests."""
    cat, _ = await TicketCategory.objects.aget_or_create(
        pk=3,
        defaults={
            "display_name": "Scoring Service Check",
            "points": 10,
            "required_fields": ["hostname", "ip_address", "service_name"],
            "optional_fields": [],
            "sort_order": 2,
        },
    )
    return cat


@pytest_asyncio.fixture
async def other_category(db: Any) -> TicketCategory:
    """Create other ticket category for bot tests."""
    cat, _ = await TicketCategory.objects.aget_or_create(
        pk=6,
        defaults={
            "display_name": "Other",
            "points": 0,
            "required_fields": ["description"],
            "optional_fields": [],
            "variable_points": True,
            "user_creatable": True,
            "sort_order": 5,
        },
    )
    return cat


@pytest.fixture
def mock_interaction() -> Any:
    """Create a mock Discord interaction."""
    interaction = AsyncMock(spec=discord.Interaction)

    # Mock user
    interaction.user = MagicMock(spec=discord.User)
    interaction.user.id = 211533935144992768
    interaction.user.name = "testuser"
    interaction.user.mention = "<@211533935144992768>"

    # Mock guild
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = 525435725123158026
    interaction.guild.name = "Test Guild"

    # Mock response
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.created_at = discord.utils.utcnow()

    return interaction


@pytest_asyncio.fixture
async def mock_admin_user(db: Any) -> User:
    import random
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    username = f"testadmin_{unique_id}"
    discord_id = random.randint(100000000000000000, 999999999999999999)

    user = await User.objects.acreate(
        username=username,
        email=f"admin_{unique_id}@test.com",
    )

    await UserGroups.objects.acreate(
        user=user,
        authentik_id=f"test-uid-{unique_id}",
        groups=[
            "WCComps_Discord_Admin",
            "WCComps_Ticketing_Admin",
        ],
    )

    await DiscordLink.objects.acreate(
        discord_id=discord_id,
        discord_username=username,
        user=user,
        is_active=True,
        team=None,
    )

    user._discord_id = discord_id
    return user


@pytest_asyncio.fixture
async def mock_team_user(db: Any) -> User:
    import random
    import uuid

    unique_id = str(uuid.uuid4())[:8]
    username = f"teammember_{unique_id}"
    discord_id = random.randint(100000000000000000, 999999999999999999)

    user = await User.objects.acreate(
        username=username,
        email=f"team_{unique_id}@test.com",
    )

    team = await Team.objects.acreate(
        team_number=(int(unique_id, 16) % 50) + 1,  # Valid range: 1-50
        team_name=f"Test Team {unique_id}",
        authentik_group=f"WCComps_BlueTeam{(int(unique_id, 16) % 50) + 1:02d}",
    )

    await UserGroups.objects.acreate(
        user=user,
        authentik_id=f"test-team-uid-{unique_id}",
        groups=[team.authentik_group],
    )

    await DiscordLink.objects.acreate(
        discord_id=discord_id,
        discord_username=username,
        user=user,
        is_active=True,
        team=team,
    )

    user._discord_id = discord_id
    return user


@pytest.fixture
def mock_bot() -> Any:
    """Create a mock Discord bot."""
    bot = AsyncMock(spec=discord.Client)
    bot.user = MagicMock()
    bot.user.id = 1422808875651829785
    bot.user.name = "wccomps-bot"

    # Mock command tree
    bot.tree = MagicMock()
    bot.tree.get_commands = MagicMock(return_value=[])
    bot.tree.add_command = MagicMock()

    return bot


@pytest.fixture
def mock_discord_guild() -> Any:
    guild = MagicMock(spec=discord.Guild)
    guild.id = 525435725123158026
    guild.name = "Test Guild"

    member1 = MagicMock(spec=discord.Member)
    member1.id = 111111111
    member1.name = "member1"
    member1.roles = []

    member2 = MagicMock(spec=discord.Member)
    member2.id = 222222222
    member2.name = "member2"
    member2.roles = []

    team1_role = MagicMock(spec=discord.Role)
    team1_role.id = 5001
    team1_role.name = "Team 01"
    team1_role.members = [member1]

    team2_role = MagicMock(spec=discord.Role)
    team2_role.id = 5002
    team2_role.name = "Team 02"
    team2_role.members = [member2]

    blueteam_role = MagicMock(spec=discord.Role)
    blueteam_role.id = 525444104763736075
    blueteam_role.name = "Blueteam"
    blueteam_role.members = [member1, member2]

    member1.roles = [team1_role, blueteam_role]
    member2.roles = [team2_role, blueteam_role]

    def get_role_by_id(role_id: int) -> Any:
        role_map = {
            5001: team1_role,
            5002: team2_role,
            525444104763736075: blueteam_role,
        }
        return role_map.get(role_id)

    guild.get_role = MagicMock(side_effect=get_role_by_id)
    guild.roles = [team1_role, team2_role, blueteam_role]

    member1.remove_roles = AsyncMock()
    member2.remove_roles = AsyncMock()

    return guild


@pytest.fixture(autouse=True)
def reset_bot_module_references() -> None:
    """Reset bot module references that may be polluted by @patch on bot.utils.

    If a module is first imported while a patch on the source module is active,
    its local binding captures the mock instead of the real function.
    """
    import bot.utils
    import core.authentik_utils

    _real_log = bot.utils.log_to_ops_channel
    _real_generate_pw = core.authentik_utils.generate_blueteam_password
    _real_parse_range = core.authentik_utils.parse_team_range

    yield

    # Restore source modules
    bot.utils.log_to_ops_channel = _real_log
    core.authentik_utils.generate_blueteam_password = _real_generate_pw
    core.authentik_utils.parse_team_range = _real_parse_range

    # Restore consumer modules
    import bot.cogs.admin_teams

    bot.cogs.admin_teams.log_to_ops_channel = _real_log
    bot.cogs.admin_teams.generate_blueteam_password = _real_generate_pw
    bot.cogs.admin_teams.parse_team_range = _real_parse_range

    import bot.cogs.admin_competition

    bot.cogs.admin_competition.log_to_ops_channel = _real_log
    bot.cogs.admin_competition.generate_blueteam_password = _real_generate_pw
    bot.cogs.admin_competition.parse_team_range = _real_parse_range


@pytest.fixture(autouse=True)
def setup_django() -> None:
    """Set up Django for tests."""
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.test_settings")
    django.setup()


@pytest.fixture(autouse=True)
def _patch_group_role_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch GROUP_ROLE_MAPPING with non-zero role IDs for all bot tests.

    Settings default to 0 when env vars are not set, which causes role
    lookups to fail in tests that create mock roles with specific IDs.
    """
    from django.conf import settings

    test_mapping = {
        "WCComps_BlackTeam": 779192640540639263,
        "WCComps_WhiteTeam": 647838503505362957,
        "WCComps_OrangeTeam": 647878925040615446,
        "WCComps_RedTeam": 647878925040615447,
        "WCComps_GoldTeam": 647878925040615448,
    }
    monkeypatch.setattr(settings, "GROUP_ROLE_MAPPING", test_mapping)
    monkeypatch.setattr(settings, "BLUETEAM_ROLE_ID", 525444104763736075)
    monkeypatch.setattr(settings, "WHITETEAM_ROLE_ID", 647838503505362957)
    monkeypatch.setattr(settings, "BLACKTEAM_ROLE_ID", 779192640540639263)
    monkeypatch.setattr(settings, "ORANGETEAM_ROLE_ID", 647878925040615446)
    monkeypatch.setattr(settings, "DISCORD_TICKET_QUEUE_CHANNEL_ID", 9999)


@pytest.fixture(scope="module")
def module_bot_world(django_db_setup: Any, django_db_blocker: Any) -> Iterator[Any]:
    """One world per test module for the bot property tests: each test runs in a savepoint inside the module's
    transaction (pytest-django's db), and each example in one more (rolled_back), all rolled back."""
    from django.db import transaction

    from bot.tests.bot_harness import build_world

    with django_db_blocker.unblock(), transaction.atomic():
        yield build_world()
        transaction.set_rollback(True)


@pytest.fixture
def bot_world(module_bot_world: Any, db: Any) -> Any:
    """The rows the bot's handlers look up (bot_harness.World)."""
    return module_bot_world
