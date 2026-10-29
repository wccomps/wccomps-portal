"""Tests for Discord manager functionality."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from django.conf import settings

from bot.discord_manager import (
    TeamInfrastructureError,
    delete_team_infrastructure,
    recreate_team_infrastructure,
    setup_team_infrastructure,
)
from team.models import Team


@pytest_asyncio.fixture
async def team(db):
    """Create a test team with unique team number."""
    import random

    team_num = random.randint(1, 50)  # Valid range: 1-50
    return await Team.objects.acreate(
        team_number=team_num,
        team_name=f"Test Team {team_num}",
        authentik_group=f"WCComps_Team{team_num:02d}",
    )


@pytest.fixture
def mock_guild_with_base_roles():
    """Create a mock Discord guild with simplified setup."""
    guild = MagicMock(spec=discord.Guild)
    guild.id = 525435725123158026
    guild.name = "Test Guild"
    guild.roles = []
    guild.categories = []

    # Counters to ensure unique IDs even after removal
    role_counter = {"value": 0}
    category_counter = {"value": 0}

    # Setup get_role method with dynamic lookup
    def get_role_by_id(role_id):
        for role in guild.roles:
            if role.id == role_id:
                return role
        return None

    guild.get_role = MagicMock(side_effect=get_role_by_id)

    # Simplified create_role
    async def create_role(**kwargs):
        role = MagicMock(spec=discord.Role)
        role.name = kwargs.get("name", "New Role")
        role.color = kwargs.get("color", discord.Color.default())
        role.id = 6000 + role_counter["value"]
        role.position = len(guild.roles)
        role.edit = AsyncMock()
        guild.roles.append(role)
        role_counter["value"] += 1
        return role

    guild.create_role = AsyncMock(side_effect=create_role)

    # Simplified create_category
    async def create_category(**kwargs):
        category = MagicMock(spec=discord.CategoryChannel)
        category.name = kwargs.get("name", "New Category")
        category.id = 7000 + category_counter["value"]
        category.position = len(guild.categories)
        category.edit = AsyncMock()
        category.create_text_channel = AsyncMock(
            return_value=MagicMock(spec=discord.TextChannel, id=8000 + category_counter["value"])
        )
        category.create_voice_channel = AsyncMock(
            return_value=MagicMock(spec=discord.VoiceChannel, id=9000 + category_counter["value"])
        )
        guild.categories.append(category)
        category_counter["value"] += 1
        return category

    guild.create_category = AsyncMock(side_effect=create_category)

    return guild


async def setup(guild: discord.Guild, team: Team) -> tuple[discord.Role, discord.CategoryChannel]:
    """Run the setup; the team's role (by its recorded ID) and category."""
    category = await setup_team_infrastructure(guild, team)
    await team.arefresh_from_db()
    return guild.get_role(team.discord_role_id), category


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestSetupTeamInfrastructure:
    """Test setup_team_infrastructure method."""

    async def test_creates_role_with_correct_name(self, team, mock_guild_with_base_roles):
        """Test that a role is created with format 'Team XX'."""
        manager = mock_guild_with_base_roles

        role, _category = await setup(manager, team)

        assert role is not None
        assert role.name == f"Team {team.team_number:02d}"

    async def test_creates_category_with_correct_name(self, team, mock_guild_with_base_roles):
        """Test that a category is created with format 'team XX'."""
        manager = mock_guild_with_base_roles

        _role, category = await setup(manager, team)

        assert category is not None
        assert category.name == f"team {team.team_number:02d}"

    async def test_white_and_orange_team_see_the_category_via_their_configured_roles(
        self, team, mock_guild_with_base_roles, monkeypatch
    ):
        guild = mock_guild_with_base_roles
        white, orange = MagicMock(spec=discord.Role, id=4101), MagicMock(spec=discord.Role, id=4102)
        white.name, orange.name = "Judges", "Volunteers"
        guild.roles.extend([white, orange])
        monkeypatch.setattr(settings, "WHITETEAM_ROLE_ID", 4101)
        monkeypatch.setattr(settings, "ORANGETEAM_ROLE_ID", 4102)

        await setup_team_infrastructure(guild, team)

        overwrites = guild.create_category.await_args.kwargs["overwrites"]
        assert overwrites[white].read_messages and overwrites[orange].send_messages

    async def test_unset_role_ids_grant_nothing_even_if_a_role_has_the_old_name(
        self, team, mock_guild_with_base_roles, monkeypatch
    ):
        guild = mock_guild_with_base_roles
        white = MagicMock(spec=discord.Role, id=4101)
        white.name = "White Team"
        guild.roles.append(white)
        monkeypatch.setattr(settings, "WHITETEAM_ROLE_ID", 0)

        await setup_team_infrastructure(guild, team)

        assert white not in guild.create_category.await_args.kwargs["overwrites"]

    async def test_creates_channels_within_category(self, team, mock_guild_with_base_roles):
        """Test that text and voice channels are created."""
        manager = mock_guild_with_base_roles

        _role, category = await setup(manager, team)

        # Verify create_text_channel was called with correct name
        category.create_text_channel.assert_called()
        call_args = category.create_text_channel.call_args
        expected_text = f"team{team.team_number:02d}-chat"
        assert expected_text in call_args[0] or call_args[0][0] == expected_text

        # Verify create_voice_channel was called with correct name
        category.create_voice_channel.assert_called()
        call_args = category.create_voice_channel.call_args
        expected_voice = f"team{team.team_number:02d}-voice"
        assert expected_voice in call_args[0] or call_args[0][0] == expected_voice

    async def test_a_refused_role_raises_with_discords_reason(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        guild.create_role.side_effect = discord.Forbidden(
            MagicMock(status=403, reason="Forbidden"), "Missing Permissions"
        )

        with pytest.raises(TeamInfrastructureError, match="Could not create the role.*Missing Permissions"):
            await setup_team_infrastructure(guild, team)

    async def test_a_failed_category_raises_with_the_reason_and_keeps_the_role(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        guild.create_category.side_effect = discord.Forbidden(
            MagicMock(status=403, reason="Forbidden"), "Missing Manage Channels"
        )

        with pytest.raises(TeamInfrastructureError, match="Could not create the category.*Missing Manage Channels"):
            await setup_team_infrastructure(guild, team)

        await team.arefresh_from_db()
        assert team.discord_role_id == guild.roles[0].id
        assert team.discord_category_id is None

    async def test_idempotent_no_duplicates_created(self, team, mock_guild_with_base_roles):
        """Test that running setup twice does not create duplicate roles or categories."""
        manager = mock_guild_with_base_roles
        guild = mock_guild_with_base_roles

        # Track initial counts
        initial_role_count = len(guild.roles)
        initial_category_count = len(guild.categories)

        # First setup
        role1, category1 = await setup(manager, team)
        assert role1 is not None
        assert category1 is not None

        # Verify one role and one category were created
        assert len(guild.roles) == initial_role_count + 1
        assert len(guild.categories) == initial_category_count + 1

        # Second setup (should be idempotent)
        role2, category2 = await setup(manager, team)
        assert role2 is not None
        assert category2 is not None

        # Verify NO additional roles or categories were created
        assert len(guild.roles) == initial_role_count + 1
        assert len(guild.categories) == initial_category_count + 1

        # Verify same instances returned
        assert role2.id == role1.id
        assert category2.id == category1.id

    async def test_self_healing_infrastructure(self, team, mock_guild_with_base_roles):
        """Test self-healing recreates missing Discord infrastructure (roles/categories)."""
        manager = mock_guild_with_base_roles
        guild = mock_guild_with_base_roles

        # Initial setup - create both role and category
        role1, category1 = await setup(manager, team)
        assert role1 is not None
        assert category1 is not None
        original_role_id = role1.id
        original_category_id = category1.id

        # Test 1: Missing category only - should recreate category, keep role
        guild.categories.remove(category1)
        role_count_before = len(guild.roles)

        role2, category2 = await setup(manager, team)

        assert role2.id == original_role_id  # Same role
        assert len(guild.roles) == role_count_before  # No new role created
        assert category2.id != original_category_id  # New category

        await team.arefresh_from_db()
        assert team.discord_role_id == original_role_id
        assert team.discord_category_id == category2.id

        # Test 2: Missing role only - should recreate role, keep category
        guild.roles.remove(role2)
        category_count_before = len(guild.categories)

        role3, category3 = await setup(manager, team)

        assert category3.id == category2.id  # Same category
        assert len(guild.categories) == category_count_before  # No new category
        assert role3.id != original_role_id  # New role

        await team.arefresh_from_db()
        assert team.discord_role_id == role3.id
        assert team.discord_category_id == category2.id

        # Test 3: Both missing - should recreate both
        guild.roles.remove(role3)
        guild.categories.remove(category3)
        role_count = len(guild.roles)
        category_count = len(guild.categories)

        role4, category4 = await setup(manager, team)

        assert role4.id != role3.id  # New role
        assert category4.id != category3.id  # New category
        assert len(guild.roles) == role_count + 1
        assert len(guild.categories) == category_count + 1

        await team.arefresh_from_db()
        assert team.discord_role_id == role4.id
        assert team.discord_category_id == category4.id


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestQueuedAnnouncements:
    """Announcements queued while a team had no chat channel go out once it has one."""

    async def _queue(self, team):
        from core.models import QueuedAnnouncement

        return await QueuedAnnouncement.objects.acreate(team=team, message="Default VM passwords", sender_name="gold")

    async def test_delivered_into_a_category_that_survived_cleanup(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        chat = MagicMock(spec=discord.TextChannel)
        chat.name = f"team{team.team_number:02d}-chat"
        chat.send = AsyncMock()
        old_category = MagicMock(spec=discord.CategoryChannel, id=7777, channels=[chat])
        old_category.name = f"team {team.team_number:02d}"
        guild.categories.append(old_category)
        guild.get_channel = MagicMock(side_effect=lambda cid: old_category if cid == 7777 else None)
        queued = await self._queue(team)

        _role, category = await setup(guild, team)

        assert category is old_category
        guild.create_category.assert_not_called()
        assert "Default VM passwords" in chat.send.await_args.args[0]
        await queued.arefresh_from_db()
        assert queued.delivered_at is not None

    async def test_delivered_into_a_new_category(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        chat = MagicMock(spec=discord.TextChannel)
        chat.name = f"team{team.team_number:02d}-chat"
        chat.send = AsyncMock()
        original = guild.create_category.side_effect

        async def create_category(**kwargs):
            category = await original(**kwargs)
            category.channels = [chat]
            category.create_text_channel = AsyncMock(return_value=chat)
            guild.get_channel = MagicMock(side_effect=lambda cid: category if cid == category.id else None)
            return category

        guild.create_category.side_effect = create_category
        queued = await self._queue(team)

        await setup_team_infrastructure(guild, team)

        await queued.arefresh_from_db()
        assert queued.delivered_at is not None
        chat.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestPartialSetup:
    """A step failing after something was created leaves its ID recorded, and a rerun finishes the job."""

    async def test_a_failed_role_position_keeps_the_new_roles_id(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        other = MagicMock(spec=discord.Role, id=5000, position=3)
        other.name = "Team 00"
        guild.roles.append(other)
        original = guild.create_role.side_effect

        async def create_role(**kwargs):
            role = await original(**kwargs)
            role.edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions"))
            return role

        guild.create_role.side_effect = create_role

        with pytest.raises(TeamInfrastructureError, match="Could not position the role.*Missing Permissions"):
            await setup_team_infrastructure(guild, team)

        await team.arefresh_from_db()
        assert team.discord_role_id == guild.roles[-1].id

    async def test_a_failed_chat_channel_keeps_the_new_categorys_id(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        original = guild.create_category.side_effect

        async def create_category(**kwargs):
            category = await original(**kwargs)
            category.channels = []
            category.create_text_channel = AsyncMock(
                side_effect=discord.Forbidden(MagicMock(status=403), "Missing Manage Channels")
            )
            return category

        guild.create_category.side_effect = create_category

        with pytest.raises(TeamInfrastructureError, match="Could not create the chat channel"):
            await setup_team_infrastructure(guild, team)

        await team.arefresh_from_db()
        assert team.discord_category_id == guild.categories[0].id

    async def test_a_rerun_adds_the_channels_a_half_built_category_lacks(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        category = MagicMock(spec=discord.CategoryChannel, id=7777, channels=[])
        category.name = f"team {team.team_number:02d}"
        category.create_text_channel = AsyncMock(return_value=MagicMock(spec=discord.TextChannel, id=8888))
        category.create_voice_channel = AsyncMock()
        guild.categories.append(category)

        await setup_team_infrastructure(guild, team)

        guild.create_category.assert_not_called()
        category.create_text_channel.assert_awaited_once()
        category.create_voice_channel.assert_awaited_once()

    async def test_a_complete_category_is_left_alone(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        chat = MagicMock(spec=discord.TextChannel)
        chat.name = f"team{team.team_number:02d}-chat"
        chat.send = AsyncMock()
        chat.edit = AsyncMock()
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.name = f"team{team.team_number:02d}-voice"
        voice.edit = AsyncMock()
        category = MagicMock(spec=discord.CategoryChannel, id=7777, channels=[chat, voice])
        category.name = f"team {team.team_number:02d}"
        category.create_text_channel = AsyncMock()
        category.create_voice_channel = AsyncMock()
        guild.categories.append(category)

        await setup_team_infrastructure(guild, team)

        category.create_text_channel.assert_not_awaited()
        category.create_voice_channel.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestReusedCategory:
    """Setup runs on every link, so on a category that exists it may only add a missing team role entry."""

    @staticmethod
    def _existing_category(team, guild, overwrites):
        chat = MagicMock(spec=discord.TextChannel)
        chat.name = f"team{team.team_number:02d}-chat"
        chat.send = AsyncMock()
        chat.set_permissions = AsyncMock()
        chat.overwrites = {}
        voice = MagicMock(spec=discord.VoiceChannel)
        voice.name = f"team{team.team_number:02d}-voice"
        voice.set_permissions = AsyncMock()
        voice.overwrites = {}
        category = MagicMock(spec=discord.CategoryChannel, id=7777, channels=[chat, voice])
        category.name = f"team {team.team_number:02d}"
        category.overwrites = overwrites
        category.set_permissions = AsyncMock()
        guild.categories.append(category)
        return category, chat, voice

    async def test_a_lost_roles_new_entry_reaches_the_category_and_its_channels(self, team, mock_guild_with_base_roles):
        """A category whose teardown failed kept an overwrite for the deleted role, so the new one couldn't see it."""
        guild = mock_guild_with_base_roles
        deleted_role = MagicMock(spec=discord.Role, id=5999)
        category, chat, voice = self._existing_category(
            team, guild, {deleted_role: discord.PermissionOverwrite(read_messages=True)}
        )

        role, _category = await setup(guild, team)

        category.set_permissions.assert_awaited_once()
        assert category.set_permissions.await_args.args[0] is role
        assert category.set_permissions.await_args.kwargs["overwrite"].read_messages
        chat.set_permissions.assert_awaited_once()
        assert chat.set_permissions.await_args.args[0] is role
        voice.set_permissions.assert_awaited_once()

    async def test_a_muted_team_role_stays_muted(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        role = await guild.create_role(name=f"Team {team.team_number:02d}")
        muted = {role: discord.PermissionOverwrite(read_messages=True, send_messages=False)}
        category, chat, _voice = self._existing_category(team, guild, muted)

        await setup_team_infrastructure(guild, team)

        category.set_permissions.assert_not_awaited()
        chat.set_permissions.assert_not_awaited()

    async def test_a_retry_fixes_a_channel_whose_permission_write_failed(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        role = await guild.create_role(name=f"Team {team.team_number:02d}")
        category, chat, voice = self._existing_category(team, guild, {})

        def apply(overwrites):
            async def set_permissions(target, *, overwrite):
                overwrites[target] = overwrite

            return set_permissions

        category.set_permissions.side_effect = apply(category.overwrites)
        chat.set_permissions.side_effect = discord.HTTPException(MagicMock(status=500), "Internal Server Error")
        voice.set_permissions.side_effect = apply(voice.overwrites)
        with pytest.raises(TeamInfrastructureError, match="chat"):
            await setup_team_infrastructure(guild, team)

        chat.set_permissions.side_effect = apply(chat.overwrites)
        await setup_team_infrastructure(guild, team)

        assert role in chat.overwrites and role in voice.overwrites and role in category.overwrites
        assert voice.set_permissions.await_count == 1

    async def test_a_channel_staff_muted_stays_muted(self, team, mock_guild_with_base_roles):
        """The category lacks the role's entry but the chat channel has one: only the voice channel gets an entry."""
        guild = mock_guild_with_base_roles
        role = await guild.create_role(name=f"Team {team.team_number:02d}")
        category, chat, voice = self._existing_category(team, guild, {})
        chat.overwrites = {role: discord.PermissionOverwrite(send_messages=False)}

        await setup_team_infrastructure(guild, team)

        category.set_permissions.assert_awaited_once()
        chat.set_permissions.assert_not_awaited()
        voice.set_permissions.assert_awaited_once()

    async def test_a_guest_role_staff_removed_stays_removed(self, team, mock_guild_with_base_roles):
        guild = mock_guild_with_base_roles
        guest = MagicMock(spec=discord.Role, id=5777)
        guest.name = "Orange Team Guest"
        guild.roles.append(guest)
        role = await guild.create_role(name=f"Team {team.team_number:02d}")
        category, _chat, _voice = self._existing_category(team, guild, {role: discord.PermissionOverwrite()})

        await setup_team_infrastructure(guild, team)

        category.set_permissions.assert_not_awaited()

    async def test_white_team_set_to_the_team_role_doesnt_override_it(
        self, team, mock_guild_with_base_roles, monkeypatch
    ):
        """A misconfigured WHITETEAM_ROLE_ID naming the team role or @everyone must not replace its overwrite."""
        guild = mock_guild_with_base_roles
        everyone = MagicMock(spec=discord.Role, id=4100)
        everyone.name = "@everyone"
        guild.default_role = everyone
        guild.roles.append(everyone)
        monkeypatch.setattr(settings, "WHITETEAM_ROLE_ID", 4100)

        await setup_team_infrastructure(guild, team)

        overwrites = guild.create_category.await_args.kwargs["overwrites"]
        assert overwrites[everyone].read_messages is False


@pytest_asyncio.fixture
async def threaded_ticket(db):
    from ticketing.models import Ticket, TicketCategory

    team = await Team.objects.acreate(team_number=23, team_name="Team 23", discord_category_id=2300)
    category = await TicketCategory.objects.acreate(pk=91, display_name="Other", points=0, sort_order=91)
    return await Ticket.objects.acreate(
        ticket_number="T023-001", team=team, category=category, title="Help", status="open", discord_thread_id=2399
    )


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_recreating_a_teams_channels_forgets_its_ticket_threads(threaded_ticket) -> None:
    chat = MagicMock(spec=discord.TextChannel)
    chat.name = "team23-chat"
    chat.delete = AsyncMock()
    category = MagicMock(spec=discord.CategoryChannel, id=2300, channels=[chat])
    category.delete = AsyncMock()
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.side_effect = lambda cid: category if cid == 2300 else None
    guild.get_role.return_value = None
    guild.roles = []

    await delete_team_infrastructure(guild, threaded_ticket.team, reason="recreate")

    await threaded_ticket.arefresh_from_db()
    assert threaded_ticket.discord_thread_id is None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_threads_are_kept_when_the_chat_channel_survives(threaded_ticket) -> None:
    chat = MagicMock(spec=discord.TextChannel)
    chat.name = "team23-chat"
    chat.delete = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions"))
    notes = MagicMock(spec=discord.TextChannel)
    notes.name = "team23-notes"
    notes.delete = AsyncMock()
    category = MagicMock(spec=discord.CategoryChannel, id=2300, channels=[chat, notes])
    category.delete = AsyncMock()
    guild = MagicMock(spec=discord.Guild)
    guild.get_channel.side_effect = lambda cid: category if cid == 2300 else None
    guild.get_role.return_value = None
    guild.roles = []

    await delete_team_infrastructure(guild, threaded_ticket.team, reason="recreate")

    await threaded_ticket.arefresh_from_db()
    assert threaded_ticket.discord_thread_id == 2399


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_recreating_never_reuses_what_it_just_deleted_while_the_cache_still_holds_it(
    team, mock_guild_with_base_roles
) -> None:
    """The gateway's delete events can arrive after the setup looks; it must create a new role and category."""
    guild = mock_guild_with_base_roles
    old_role, old_category = await setup(guild, team)
    old_role.delete = AsyncMock()
    old_category.delete = AsyncMock()
    old_category.channels = []
    guild.get_channel = MagicMock(side_effect=lambda cid: next((c for c in guild.categories if c.id == cid), None))

    deleted, category = await recreate_team_infrastructure(guild, team, reason="recreate")

    await team.arefresh_from_db()
    assert deleted == ["category", "role"]
    assert category is not old_category
    assert team.discord_category_id == category.id != old_category.id
    assert team.discord_role_id not in (None, old_role.id)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_role_whose_position_edit_failed_is_placed_on_the_retry(mock_guild_with_base_roles) -> None:
    """Only the first attempt created the role; the retry finds it by ID and still places it, then leaves it."""
    guild = mock_guild_with_base_roles
    team = await Team.objects.acreate(team_number=20, team_name="Team 20")
    await guild.create_role(name="Team 19")
    await guild.create_role(name="Blue Team")
    create, edits = guild.create_role.side_effect, []

    async def create_with_flaky_edit(**kwargs):
        role = await create(**kwargs)

        async def edit(*, position):
            edits.append(position)
            if len(edits) == 1:
                raise discord.HTTPException(MagicMock(status=500), "Internal Server Error")
            role.position = position

        role.edit = AsyncMock(side_effect=edit)
        return role

    guild.create_role.side_effect = create_with_flaky_edit
    with pytest.raises(TeamInfrastructureError, match="position the role"):
        await setup_team_infrastructure(guild, team)
    await setup_team_infrastructure(guild, team)
    await setup_team_infrastructure(guild, team)

    assert edits == [1, 1]


PANEL_BOT_USER = MagicMock(spec=discord.ClientUser, id=880088)


def _panel_bot(posting_error: Exception | None = None) -> tuple[MagicMock, MagicMock]:
    from discord.ext import commands

    from bot.cogs.help_panels import HelpPanelsCog

    cog = MagicMock(spec=HelpPanelsCog)

    cog.post_team_ticket_panel = AsyncMock(side_effect=posting_error)
    bot = MagicMock(spec=commands.Bot)
    bot.get_cog.return_value = cog
    bot.user = PANEL_BOT_USER
    return bot, cog


def _history(*messages: MagicMock) -> MagicMock:
    async def each(**_kwargs):
        for message in messages:
            yield message

    return MagicMock(side_effect=each)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestTicketPanel:
    """The panel is posted whenever the chat channel lacks the bot's, so a lost post is redone on the next setup."""

    async def _chat(self, team, guild) -> MagicMock:
        role = await guild.create_role(name=f"Team {team.team_number:02d}")
        _category, chat, _voice = TestReusedCategory._existing_category(
            team, guild, {role: discord.PermissionOverwrite()}
        )
        return chat

    async def test_a_chat_channel_without_the_panel_gets_it(self, team, mock_guild_with_base_roles):
        chat = await self._chat(team, mock_guild_with_base_roles)
        chat.history = _history()
        bot, cog = _panel_bot()

        await setup_team_infrastructure(mock_guild_with_base_roles, team, bot=bot)

        cog.post_team_ticket_panel.assert_awaited_once_with(chat.id)

    async def test_a_chat_channel_with_the_bots_panel_is_left_alone(self, team, mock_guild_with_base_roles):
        chat = await self._chat(team, mock_guild_with_base_roles)
        button = MagicMock(custom_id="help_panel:ticket")
        chat.history = _history(MagicMock(author=PANEL_BOT_USER, components=[MagicMock(children=[button])]))
        bot, cog = _panel_bot()

        await setup_team_infrastructure(mock_guild_with_base_roles, team, bot=bot)

        cog.post_team_ticket_panel.assert_not_awaited()

    async def test_a_failed_panel_post_fails_the_setup_so_it_is_retried(self, team, mock_guild_with_base_roles):
        chat = await self._chat(team, mock_guild_with_base_roles)
        chat.history = _history()
        bot, _cog = _panel_bot(discord.HTTPException(MagicMock(status=500), "Internal Server Error"))

        with pytest.raises(TeamInfrastructureError, match="ticket panel"):
            await setup_team_infrastructure(mock_guild_with_base_roles, team, bot=bot)
