"""Driving every bot entry point with hostile input, as every kind of Discord user, and checking what the bot sends.

A row (Command, Item, Modal or Listener) says how to reach one entry point: the slash command's options (read from
the command itself: their types, choices and the limits Discord enforces, with hints for values that get past
lookups), the view a button or select sits on, a modal's text inputs (their limits too), or the message a listener
gets. Discord itself is a set of fakes that record everything the bot sends. The properties (check_run) hold for
every row:

1. The handler returns: no exception. An interaction is answered (a reply, a modal, or a deferral and its
   follow-up), so no one sees "The application did not respond".
2. Everything sent fits Discord's limits (content, embeds, components, modals, thread names, autocomplete choices).
3. No mention escapes: no message may ping @everyone, @here, every role or every user it names; only the ones the
   bot lists in allowed_mentions (merged over the client's default, as discord.py does).
4. Refused means unchanged: a role the row doesn't allow, or an input the bot refuses outright, writes no row.

test_property_coverage fails when a bot entry point has no row.
"""

import inspect
import itertools
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
from asgiref.sync import async_to_sync
from discord import app_commands
from discord.ext import commands, tasks
from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from hypothesis import strategies as st

from core.models import CompetitionConfig
from core.permission_constants import check_groups_for_permission
from core.tests.roles import ROLE_GROUPS, make_user
from core.tests.strategies import file_bytes, file_names, request_text, rolled_back
from core.tests.views_harness import offline, table_writes
from team.models import DiscordLink, Team
from ticketing.models import Ticket, TicketCategory

REPO_DIR = Path(__file__).resolve().parents[2]


class Draws(Protocol):
    """What drive draws its inputs from: Hypothesis's data, or Chosen values in a regression test."""

    def draw(self, strategy: st.SearchStrategy[Any], label: str | None = None) -> Any: ...


@cache
def portal_bot() -> commands.Bot:
    """The bot as main.py makes it: the fakes take its default allowed mentions and its command tree's class.
    Imported when first needed, as main.py sets up Django and logging when imported."""
    from main import PortalBot

    return PortalBot()


# ---------------------------------------------------------------------------------------------------------------
# Who is clicking
# ---------------------------------------------------------------------------------------------------------------

# Each kind of Discord user and the Authentik groups of the account their DiscordLink points at; None is a Discord
# user who never linked. blue_team is on team 1, other_team on team 2.
BOT_ROLES: dict[str, list[str] | None] = {
    "unlinked": None,
    "no_role": ROLE_GROUPS["no_role"],
    "blue_team": ROLE_GROUPS["blue_team"],
    "other_team": ["WCComps_BlueTeam02"],
    "gold_team": ROLE_GROUPS["gold_team"],
    "ticketing_support": ROLE_GROUPS["ticketing_support"],
    "ticketing_admin": ROLE_GROUPS["ticketing_admin"],
    "admin": ROLE_GROUPS["admin"],
}
EVERYONE = frozenset(BOT_ROLES)
TEAM_MEMBERS = frozenset({"blue_team", "other_team"})


def may(permission: str) -> frozenset[str]:
    """The roles whose groups grant the permission (core.permission_constants)."""
    return frozenset(
        role
        for role, groups in BOT_ROLES.items()
        if groups is not None and check_groups_for_permission(groups, permission)
    )


# ---------------------------------------------------------------------------------------------------------------
# The rows the handlers look up
# ---------------------------------------------------------------------------------------------------------------

GUILD_ID = 525435725123158026
BOT_USER_ID = 1422808875651829785
LOG_CHANNEL_ID = 900_001
QUEUE_CHANNEL_ID = 900_002
ANNOUNCEMENT_CHANNEL_ID = 900_003
LINK_CHANNEL_ID = 900_004
TEAM_CATEGORY_ID = 910_001
TEAM_CHAT_ID = 910_002
TEAM_ROLE_ID = 920_001
_ids = itertools.count(8_100_000_000)


@dataclass(repr=False)
class World:
    users: dict[str, User]  # linked roles
    discord_ids: dict[str, int]  # every role's Discord id (the unlinked one's too)
    teams: list[Team]  # teams 1-3; team 1 has a role and a category with a chat channel
    tickets: dict[str, Ticket]  # by name: open, claimed, resolved (team 1), other (team 2's open one)
    categories: list[int]
    user_creatable: list[int]

    def __repr__(self) -> str:
        return "World()"

    @property
    def ticket_numbers(self) -> list[str]:
        return [t.ticket_number for t in self.tickets.values()]

    @property
    def thread_ids(self) -> list[int]:
        return [t.discord_thread_id for t in self.tickets.values() if t.discord_thread_id]


def build_world() -> World:
    teams = [
        Team.objects.update_or_create(
            team_number=n,
            defaults={
                "team_name": f"Team {n:02d}",
                "authentik_group": f"WCComps_BlueTeam{n:02d}",
                "discord_role_id": TEAM_ROLE_ID if n == 1 else None,
                "discord_category_id": TEAM_CATEGORY_ID if n == 1 else None,
            },
        )[0]
        for n in (1, 2, 3)
    ]
    users = {}
    discord_ids = {}
    for role, groups in BOT_ROLES.items():
        if groups is None:
            discord_ids[role] = next(_ids)
            continue
        user = make_user(f"bot-{role}", groups)
        users[role] = user
        discord_ids[role] = DiscordLink.objects.get(user=user).discord_id
    DiscordLink.objects.filter(user=users["blue_team"]).update(team=teams[0])
    DiscordLink.objects.filter(user=users["other_team"]).update(team=teams[1])

    category = TicketCategory.objects.get(pk=6)
    tickets = {}
    for name, team, status, number in (
        ("open", teams[0], "open", "T001-001"),
        ("claimed", teams[0], "claimed", "T001-002"),
        ("resolved", teams[0], "resolved", "T001-003"),
        ("other", teams[1], "open", "T002-001"),
    ):
        tickets[name] = Ticket.objects.create(
            team=team,
            category=category,
            ticket_number=number,
            title=f"Ticket {number}",
            description="Seen",
            status=status,
            assigned_to=users["ticketing_support"] if status != "open" else None,
            assigned_at=timezone.now() - timedelta(hours=3) if status != "open" else None,
            resolved_at=timezone.now() if status == "resolved" else None,
            points_charged=5 if status == "resolved" else 0,
            discord_thread_id=next(_ids),
        )
    Team.objects.filter(pk=teams[0].pk).update(ticket_counter=3)
    Team.objects.filter(pk=teams[1].pk).update(ticket_counter=1)
    CompetitionConfig.get_config()
    return World(
        users=users,
        discord_ids=discord_ids,
        teams=teams,
        tickets=tickets,
        categories=list(TicketCategory.objects.order_by("pk").values_list("pk", flat=True)),
        user_creatable=list(
            TicketCategory.objects.filter(user_creatable=True).order_by("pk").values_list("pk", flat=True)
        ),
    )


def stored_text(max_length: int) -> st.SearchStrategy[str]:
    """Text a column holds: anything the web or the bot let in, within the column's length (never a NUL, which
    PostgreSQL text can't hold)."""
    return request_text(max_length).filter(lambda s: "\x00" not in s and len(s) <= max_length)


def seed_hostile(world: World, data: Draws) -> None:
    """Rewrite what the handlers show (team, category and user names, tickets' text) with hostile text, and add
    tickets across the categories, so replies, embeds and the dashboard are built from it. Half the examples keep
    the plain world. A few strings are drawn and used everywhere, each cut to its column."""
    if not data.draw(st.booleans(), label="hostile rows"):
        return
    name = data.draw(stored_text(100), label="a name")
    text = data.draw(stored_text(5000), label="a text")
    Team.objects.filter(pk__in=[t.pk for t in world.teams]).update(team_name=name.strip() or "Team")
    TicketCategory.objects.filter(pk__in=world.categories).update(
        display_name=name.strip() or "Category", playbook=text[:3000]
    )
    DiscordLink.objects.filter(is_active=True).update(discord_username=text[:255])
    User.objects.filter(pk=world.users["ticketing_support"].pk).update(username=text[:150].strip() or "volunteer")
    Ticket.objects.filter(pk__in=[t.pk for t in world.tickets.values()]).update(
        title=text[:255],
        description=text,
        hostname=text[:255],
        service_name=name,
        ip_address=text[:45],
        resolution_notes=text,
    )
    TicketCategory.objects.bulk_create(
        TicketCategory(display_name=f"{n} {name}"[:100].strip() or "Category", sort_order=100 + n, user_creatable=True)
        for n in range(data.draw(st.integers(0, 30), label="more categories"))
    )
    extra = data.draw(st.integers(0, 40), label="more tickets")
    Ticket.objects.bulk_create(
        Ticket(
            team=world.teams[0],
            category_id=world.categories[n % len(world.categories)],
            ticket_number=f"T001-{100 + n:03d}",
            title=f"More {n}",
            description=text,
            status="open" if n % 2 else "claimed",
            assigned_to=world.users["ticketing_support"] if n % 2 == 0 else None,
            assigned_at=timezone.now() if n % 2 == 0 else None,
            discord_thread_id=8_200_000_000 + n,
        )
        for n in range(extra)
    )


# ---------------------------------------------------------------------------------------------------------------
# Discord, faked: everything the bot sends is recorded
# ---------------------------------------------------------------------------------------------------------------


@dataclass
class Sent:
    """One thing the bot sent to Discord: a message, an edit, a modal, a thread or autocomplete choices."""

    where: str
    method: str
    content: Any = None
    embeds: list[discord.Embed] = field(default_factory=list)
    view: discord.ui.View | None = None
    modal: discord.ui.Modal | None = None
    files: list[discord.File] = field(default_factory=list)
    allowed_mentions: discord.AllowedMentions | None = None
    name: str | None = None  # a thread's
    choices: list[app_commands.Choice[Any]] | None = None


class NotAnsweredError(Exception):
    """A follow-up or an edit sent before the interaction was answered: Discord refuses it (Unknown Webhook)."""


def _sent(where: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Sent:
    embeds = list(kwargs.get("embeds") or [])
    if kwargs.get("embed") is not None:
        embeds.append(kwargs["embed"])
    files = list(kwargs.get("files") or [])
    if kwargs.get("file") is not None:
        files.append(kwargs["file"])
    return Sent(
        where=where,
        method=method,
        content=args[0] if args else kwargs.get("content"),
        embeds=embeds,
        view=kwargs.get("view"),
        files=files,
        allowed_mentions=kwargs.get("allowed_mentions"),
        name=kwargs.get("name"),
    )


class FakeResponse:
    """interaction.response: answered once (a second answer raises discord.InteractionResponded)."""

    def __init__(self, discord_: Discord, interaction: Any) -> None:
        self._discord = discord_
        self._interaction = interaction
        self.component = False
        self.answer: str | None = None

    def is_done(self) -> bool:
        return self.answer is not None

    def _answer(self, how: str) -> None:
        if self.answer is not None:
            raise discord.InteractionResponded(self._interaction)
        self.answer = how

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        self._answer("message")
        self._discord.sent.append(_sent("reply", "send_message", args, kwargs))

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        # A component's deferral without "thinking" acknowledges the click and is the whole answer; a command's (or
        # a thinking one) shows "thinking..." until a follow-up comes
        self._answer("acknowledged" if self.component and not thinking else "deferred")

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._answer("modal")
        self._discord.sent.append(Sent(where="reply", method="send_modal", modal=modal))

    async def edit_message(self, *args: Any, **kwargs: Any) -> None:
        self._answer("edit")
        self._discord.sent.append(_sent("reply", "edit_message", args, kwargs))

    async def autocomplete(self, choices: list[app_commands.Choice[Any]]) -> None:
        self._answer("autocomplete")
        self._discord.sent.append(Sent(where="reply", method="autocomplete", choices=list(choices)))


class FakeFollowup:
    def __init__(self, discord_: Discord, response: FakeResponse) -> None:
        self._discord = discord_
        self._response = response

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        if not self._response.is_done():
            raise NotAnsweredError("a follow-up before the interaction was answered")
        self._discord.sent.append(_sent("followup", "followup.send", args, kwargs))
        return self._discord.message_object()


class Discord:
    """The guild, its channels and roles, the bot's client, and every user the world knows, as fakes."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.sent: list[Sent] = []
        self._next_id = itertools.count(930_000)
        self.bot = self._bot()
        self.guild = self._guild()
        self.bot.get_guild.side_effect = lambda gid: self.guild
        self.cogs: dict[str, commands.Cog] = {}

    # Building blocks

    def recorder(self, where: str, method: str, returns: Any = None) -> AsyncMock:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.sent.append(_sent(where, method, args, kwargs))
            return returns() if callable(returns) else returns

        return AsyncMock(side_effect=record)

    def message_object(self, channel: Any = None) -> Any:
        """A message the bot sent (what send() returns): an id, and edits, pins and reactions recorded."""
        message = MagicMock(spec=discord.Message)
        message.id = next(self._next_id)
        message.channel = channel
        message.embeds = []
        message.edit = self.recorder("sent message", "edit")
        message.pin = AsyncMock()
        message.delete = AsyncMock()
        message.add_reaction = AsyncMock()
        return message

    def user(self, discord_id: int, name: str = "player", *, bot: bool = False) -> Any:
        user = MagicMock(spec=discord.Member)
        user.id = discord_id
        user.name = name
        user.display_name = name
        user.global_name = name
        user.mention = f"<@{discord_id}>"
        user.bot = bot
        user.roles = []
        user.__str__.return_value = name  # type: ignore[attr-defined]
        user.send = self.recorder(f"DM to {name}", "send", returns=self.message_object)
        user.add_roles = AsyncMock()
        user.remove_roles = AsyncMock()
        return user

    def member_for(self, role: str) -> Any:
        return self.user(self.world.discord_ids[role], name=f"{role}_player")

    def text_channel(self, channel_id: int, name: str, category: Any = None) -> Any:
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = channel_id
        channel.name = name
        channel.mention = f"<#{channel_id}>"
        channel.guild = getattr(self, "guild", None)
        channel.category = category
        channel.send = self.recorder(f"#{name}", "send", returns=lambda: self.message_object(channel))
        channel.create_thread = self.recorder(
            f"#{name}", "create_thread", returns=lambda: self.thread(next(self._next_id))
        )
        channel.fetch_message = AsyncMock(side_effect=lambda mid: self.message_object(channel))
        channel.delete = AsyncMock()

        async def history(**_: Any) -> Any:
            for message in ():
                yield message

        channel.history = history
        return channel

    def thread(self, thread_id: int, *, parent: Any = None) -> Any:
        thread = MagicMock(spec=discord.Thread)
        thread.id = thread_id
        thread.name = f"thread-{thread_id}"
        thread.mention = f"<#{thread_id}>"
        thread.parent = parent
        thread.send = self.recorder(f"thread {thread_id}", "send", returns=lambda: self.message_object(thread))
        thread.edit = self.recorder(f"thread {thread_id}", "edit")
        thread.add_user = AsyncMock()
        thread.delete = AsyncMock()
        return thread

    def role(self, role_id: int, name: str) -> Any:
        role = MagicMock(spec=discord.Role)
        role.id = role_id
        role.name = name
        role.mention = f"<@&{role_id}>"
        role.position = 1
        role.members = []
        role.color = discord.Color.default()
        role.is_assignable.return_value = True
        role.edit = AsyncMock()
        role.delete = AsyncMock()
        return role

    def category(self, category_id: int, name: str) -> Any:
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = category_id
        category.name = name
        category.position = 1
        category.channels = []
        category.delete = AsyncMock()
        category.edit = AsyncMock()

        async def create_text_channel(name: str, **_: Any) -> Any:
            channel = self.text_channel(next(self._next_id), name, category)
            category.channels.append(channel)
            self._channels[channel.id] = channel
            return channel

        async def create_voice_channel(name: str, **_: Any) -> Any:
            channel = MagicMock(spec=discord.VoiceChannel)
            channel.id = next(self._next_id)
            channel.name = name
            channel.delete = AsyncMock()
            category.channels.append(channel)
            return channel

        category.create_text_channel = AsyncMock(side_effect=create_text_channel)
        category.create_voice_channel = AsyncMock(side_effect=create_voice_channel)
        return category

    def _bot(self) -> Any:
        bot = MagicMock(spec=commands.Bot)
        bot.user = self.user(BOT_USER_ID, "wccomps-bot", bot=True)
        bot.allowed_mentions = portal_bot().allowed_mentions
        bot.unified_dashboard = None
        bot.get_channel.side_effect = lambda cid: self.channel(cid)
        bot.get_cog.side_effect = lambda name: self.cogs.get(name)
        bot.wait_until_ready = AsyncMock()

        async def fetch_user(user_id: int) -> Any:
            known = {discord_id: role for role, discord_id in self.world.discord_ids.items()}
            if user_id in known:
                return self.member_for(known[user_id])
            raise discord.NotFound(MagicMock(status=404, reason="Not Found"), "Unknown User")

        bot.fetch_user = AsyncMock(side_effect=fetch_user)
        bot.loop = MagicMock()
        bot.loop.create_task.side_effect = lambda coro: coro.close()
        bot._connection = MagicMock()
        bot.http = MagicMock()
        bot._connection._command_tree = None
        return bot

    def _guild(self) -> Any:
        guild = MagicMock(spec=discord.Guild)
        guild.id = GUILD_ID
        guild.name = "Competition"
        guild.chunked = True
        guild.me.guild_permissions.manage_roles = True
        guild.default_role = self.role(GUILD_ID, "@everyone")
        team_role = self.role(TEAM_ROLE_ID, "Team 01")
        blueteam = self.role(settings.BLUETEAM_ROLE_ID, "Blueteam")
        guild.roles = [guild.default_role, team_role, blueteam]
        self.guild = guild
        team_category = self.category(TEAM_CATEGORY_ID, "team 01")
        chat = self.text_channel(TEAM_CHAT_ID, "team01-chat", team_category)
        team_category.channels.append(chat)
        guild.categories = [team_category]
        self._channels: dict[int, Any] = {TEAM_CATEGORY_ID: team_category, TEAM_CHAT_ID: chat}
        # The rest are made when first looked up: most handlers touch few of them, and each fake costs time
        self._made_on_lookup: dict[int, Callable[[], Any]] = {
            LOG_CHANNEL_ID: lambda: self.text_channel(LOG_CHANNEL_ID, "ops-log"),
            QUEUE_CHANNEL_ID: lambda: self.text_channel(QUEUE_CHANNEL_ID, "ticket-queue"),
            ANNOUNCEMENT_CHANNEL_ID: lambda: self.text_channel(ANNOUNCEMENT_CHANNEL_ID, "announcements"),
            LINK_CHANNEL_ID: lambda: self.text_channel(LINK_CHANNEL_ID, "link"),
            **{tid: (lambda tid=tid: self.thread(tid, parent=chat)) for tid in self.world.thread_ids},
        }
        guild.get_channel.side_effect = self.channel
        guild.get_role.side_effect = lambda rid: next((r for r in guild.roles if r.id == rid), None)
        roles_by_id = {discord_id: role for role, discord_id in self.world.discord_ids.items()}
        members: dict[int, Any] = {}

        def get_member(discord_id: int) -> Any:
            if discord_id in roles_by_id and discord_id not in members:
                members[discord_id] = self.user(discord_id, f"{roles_by_id[discord_id]}_player")
            return members.get(discord_id)

        guild.get_member.side_effect = get_member
        type(guild).members = PropertyMock(side_effect=lambda: [get_member(i) for i in roles_by_id])

        async def create_role(name: str, **_: Any) -> Any:
            role = self.role(next(self._next_id), name)
            guild.roles.append(role)
            return role

        async def create_category(name: str, **_: Any) -> Any:
            category = self.category(next(self._next_id), name)
            guild.categories.append(category)
            self._channels[category.id] = category
            return category

        guild.create_role = AsyncMock(side_effect=create_role)
        guild.create_category = AsyncMock(side_effect=create_category)
        return guild

    def channel(self, channel_id: int) -> Any:
        """The channel or thread with this id, made when first looked up; None for one the guild doesn't have."""
        if channel_id not in self._channels and channel_id in self._made_on_lookup:
            self._channels[channel_id] = self._made_on_lookup.pop(channel_id)()
        return self._channels.get(channel_id)

    # What a handler gets

    def interaction(
        self, role: str, *, message: Any = None, data: dict[str, Any] | None = None, component: bool = False
    ) -> Any:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.id = next(self._next_id)
        interaction.user = self.member_for(role)
        interaction.guild = self.guild
        interaction.guild_id = GUILD_ID
        interaction.channel = self.channel(TEAM_CHAT_ID)
        interaction.client = self.bot
        interaction.message = message
        interaction.data = data or {}
        response = FakeResponse(self, interaction)
        response.component = component
        interaction.response = response
        interaction.followup = FakeFollowup(self, response)

        async def edit_original_response(**kwargs: Any) -> Any:
            if not response.is_done():
                raise NotAnsweredError("an edit before the interaction was answered")
            self.sent.append(_sent("reply", "edit_original_response", (), kwargs))
            return self.message_object()

        interaction.edit_original_response = AsyncMock(side_effect=edit_original_response)
        return interaction

    def answered(self, interaction: Any) -> bool:
        """The interaction got its answer: a reply, a modal, or a deferral followed by a follow-up or an edit."""
        response: FakeResponse = interaction.response
        if response.answer is None:
            return False
        if response.answer != "deferred":
            return True
        return any(s.where in ("followup", "reply") for s in self.sent)


# ---------------------------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------------------------

DISCORD_INT = 2**53  # Discord's integer options range over ±2**53
STRING_OPTION_MAX = 6000


def option_text(min_length: int = 0, max_length: int = STRING_OPTION_MAX) -> st.SearchStrategy[str]:
    """Text Discord delivers for a string option or a text input: anything within its declared length (longer text
    is cut to it, so the hostile start of a long value stays, rather than drawn again)."""
    return request_text(max_length).map(lambda s: s[:max_length]).filter(lambda s: len(s) >= min_length)


Hint = Callable[[World], st.SearchStrategy[Any]]


def mentions_of(world: World) -> st.SearchStrategy[str]:
    ids = sorted(world.discord_ids.values())
    return st.lists(
        st.one_of(
            st.sampled_from(ids).map(lambda i: f"<@{i}>"),
            st.sampled_from(ids).map(lambda i: f"<@!{i}>"),
            st.sampled_from(ids).map(str),
            st.sampled_from(("99999999999999999999", "123456789012345678", "<@0>", "@everyone")),
        ),
        min_size=1,
        max_size=4,
    ).map(" ".join)


def team_ranges() -> st.SearchStrategy[str]:
    return st.one_of(
        st.sampled_from(("1", "1,2", "1-3", "2-3,1", "3,3", "50", "4", "0", "51", "1-50", "3-1", "1-", "-1", "a-b")),
        st.lists(st.integers(-5, 60).map(str), min_size=1, max_size=4).map(",".join),
    )


def category_ids(world: World) -> st.SearchStrategy[str]:
    return st.one_of(st.sampled_from(world.categories).map(str), st.sampled_from(("0", "-1", "999999", "2147483648")))


def ticket_numbers(world: World) -> st.SearchStrategy[str]:
    return st.sampled_from([*world.ticket_numbers, "T999-999"])


# ---------------------------------------------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------------------------------------------


def _entry_point(kind: str, obj: Any) -> str:
    path = Path(inspect.getsourcefile(obj) or "").resolve().relative_to(REPO_DIR).as_posix()
    return f"bot:{kind}:{path}:{obj.__qualname__}"


@dataclass(frozen=True, repr=False)
class Command:
    """A slash command: its options come from the command itself (types, choices, Discord's limits)."""

    cog: type[commands.Cog]
    callback: Callable[..., Awaitable[None]]
    allowed: frozenset[str]
    hints: dict[str, Hint] = field(default_factory=dict)  # option -> valid values that get past lookups

    @property
    def entry_point(self) -> str:
        return _entry_point("command", self.callback)

    @property
    def name(self) -> str:
        return self.callback.__qualname__

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True, repr=False)
class Item:
    """A button or select on a view: the view is built, the item found and clicked (a select's values chosen from its
    options, as Discord only sends those)."""

    kind: str  # "button" or "select"
    target: Any  # the item's class, or the view method a @discord.ui.button decorates
    view: Callable[[Run, Draws], discord.ui.View]
    allowed: frozenset[str]
    # The roles that ever get this item (a modal's or a confirmation's buttons go only to the one who asked)
    reached_by: frozenset[str] = EVERYONE
    message: Callable[[Run, Draws], Any] | None = None  # the message the item is on

    @property
    def entry_point(self) -> str:
        return _entry_point(self.kind, self.target)

    @property
    def name(self) -> str:
        return self.target.__qualname__

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True, repr=False)
class Modal:
    """A modal: built as the bot builds it, its text inputs filled within the limits Discord enforces."""

    modal: type[discord.ui.Modal]
    build: Callable[[Run, Draws], discord.ui.Modal]
    allowed: frozenset[str]
    reached_by: frozenset[str] = EVERYONE
    hints: dict[str, Hint] = field(default_factory=dict)  # text input label -> valid values

    @property
    def entry_point(self) -> str:
        return _entry_point("modal", self.modal)

    @property
    def name(self) -> str:
        return self.modal.__qualname__

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True, repr=False)
class Listener:
    """A message listener: the message (its author, channel, content and attachments) is drawn."""

    cog: type[commands.Cog]
    callback: Callable[..., Awaitable[None]]
    # Draws the message (and sets up the rows it needs), returning the call of the listener with it
    prepare: Callable[[Run, st.DataObject, Any], Callable[[], Awaitable[None]]]
    allowed: frozenset[str] = EVERYONE

    @property
    def entry_point(self) -> str:
        return _entry_point("listener", self.callback)

    @property
    def name(self) -> str:
        return self.callback.__qualname__

    def __repr__(self) -> str:
        return self.name

    @property
    def function(self) -> str:
        """The listener as the parser guard names a function (path:qualname)."""
        return self.entry_point.removeprefix("bot:listener:")


BotCase = Command | Item | Modal | Listener


# ---------------------------------------------------------------------------------------------------------------
# Running one example
# ---------------------------------------------------------------------------------------------------------------


@dataclass
class Run:
    """One example: the world, the fakes, the cogs and what the handler was given."""

    world: World
    discord: Discord
    role: str = ""
    interaction: Any = None
    refused: bool = False  # refused before the handler: a check, or the command tree's guard
    texts: list[str] = field(default_factory=list)  # the user text sent
    written: list[str] = field(default_factory=list)  # the tables the handler wrote to

    def cog(self, cls: type[commands.Cog]) -> Any:
        return next(c for c in self.discord.cogs.values() if isinstance(c, cls))


def _load_cogs(discord_: Discord) -> None:
    from bot.cogs.admin import AdminCog
    from bot.cogs.admin_competition import AdminCompetitionCog
    from bot.cogs.admin_teams import AdminTeamsCog
    from bot.cogs.admin_tickets import AdminTicketsCog
    from bot.cogs.help_panels import HelpPanelsCog
    from bot.cogs.linking import LinkingCog
    from bot.cogs.scoring import ScoringCog
    from bot.cogs.ticketing import TicketingCog

    for cls in (
        AdminCog,
        AdminCompetitionCog,
        AdminTeamsCog,
        AdminTicketsCog,
        HelpPanelsCog,
        LinkingCog,
        ScoringCog,
        TicketingCog,
    ):
        discord_.cogs[cls.__name__] = cls(discord_.bot)


def _inline_competition_run(enable: bool, actor: str, on_step: Any = None) -> Any:
    """bot.competition_actions.run_competition on the test's own connection (it runs on a pool thread with one of
    its own in production, which would see none of the example's rows)."""
    from asgiref.sync import sync_to_async

    from core.services.competition import run_competition_to_completion

    return sync_to_async(run_competition_to_completion)(enable, actor, on_step)


@contextmanager
def discord_world(world: World, confirm: bool | None) -> Iterator[Discord]:
    """The fakes, every other service offline, the bot's Discord settings, and every confirmation answered with
    `confirm` (True, False, or None for a timeout)."""
    from bot.utils import ConfirmView

    async def answered_confirmation(self: ConfirmView) -> bool:
        self.confirmed = confirm
        return confirm is None

    discord_ = Discord(world)
    with ExitStack() as stack:
        stack.enter_context(offline())
        stack.enter_context(
            override_settings(
                AUTHENTIK_TOKEN="property-token",
                DISCORD_LOG_CHANNEL_ID=LOG_CHANNEL_ID,
                DISCORD_TICKET_QUEUE_CHANNEL_ID=QUEUE_CHANNEL_ID,
                DISCORD_ANNOUNCEMENT_CHANNEL_ID=ANNOUNCEMENT_CHANNEL_ID,
                DISCORD_LINK_CHANNEL_ID=LINK_CHANNEL_ID,
                COMPETITION_GUILD_ID=GUILD_ID,
            )
        )
        stack.enter_context(patch.object(tasks.Loop, "start"))
        stack.enter_context(patch.object(ConfirmView, "wait", answered_confirmation))
        stack.enter_context(patch("bot.competition_actions.run_competition", _inline_competition_run))
        stack.enter_context(patch("quotient.client.get_quotient_client.cache_clear"))
        yield discord_


def command_tree(discord_: Discord) -> app_commands.CommandTree:
    """A command tree of the class the bot uses, which every slash command goes through first."""
    discord_.bot._connection._command_tree = None
    return type(portal_bot().tree)(discord_.bot)


def bound_command(run: Run, case: Command) -> app_commands.Command[Any, ..., Any]:
    cog = run.cog(case.cog)
    for command in cog.walk_app_commands():
        if isinstance(command, app_commands.Command) and command.callback is case.callback:
            return command
    raise AssertionError(f"{case.name} is not a command of {case.cog.__name__}")


def _option_strategy(case: Command, param: Any, world: World) -> st.SearchStrategy[Any]:
    hint = case.hints.get(param.name)
    if param.choices:
        return st.sampled_from([c.value for c in param.choices])
    match param.type:
        case discord.AppCommandOptionType.string:
            low = param.min_value or 0
            high = param.max_value or STRING_OPTION_MAX
            text = option_text(low, high)
            if hint is not None:
                return st.one_of(hint(world).filter(lambda s: low <= len(s) <= high), text)
            return text
        case discord.AppCommandOptionType.integer:
            low = param.min_value if param.min_value is not None else -DISCORD_INT
            high = param.max_value if param.max_value is not None else DISCORD_INT
            bounds = [v for v in (0, 1, -1, 2**31 - 1, 2**31, 2**63, DISCORD_INT, -DISCORD_INT) if low <= v <= high]
            numbers = st.one_of(st.integers(low, high), st.sampled_from(bounds or [low]))
            return st.one_of(hint(world), numbers) if hint is not None else numbers
        case discord.AppCommandOptionType.boolean:
            return st.booleans()
    raise AssertionError(f"{case.name}: no strategy for option {param.name} of type {param.type}")


def _options_data(command: app_commands.Command[Any, ..., Any], values: dict[str, Any]) -> dict[str, Any]:
    """The interaction's data, as Discord sends it: the options nested under the command's groups."""
    data: dict[str, Any] = {
        "name": command.name,
        "type": 1,
        "options": [{"name": k, "type": 3, "value": v} for k, v in values.items()],
    }
    parent = command.parent
    while parent is not None:
        data = {"name": parent.name, "type": 1, "options": [{**data, "type": 1}]}
        parent = parent.parent
    return data


Go = Callable[[], Awaitable[None]]


def prepare_command(run: Run, case: Command, data: Draws) -> Go:
    """Draw the options (and what the user types into each autocompleted one), then run the tree's guard and
    autocompletes, followed by the tree's guard, the command's checks, its transformers and its callback, as
    discord.py does."""
    command = bound_command(run, case)
    values: dict[str, Any] = {}
    for param in command.parameters:
        strategy = _option_strategy(case, param, run.world)
        if not param.required and data.draw(st.booleans(), label=f"omit {param.name}"):
            continue
        values[param.name] = data.draw(strategy, label=param.name)
    typed = {
        param.name: data.draw(option_text(0, 100), label=f"typing {param.name}")
        for param in command.parameters
        if param.autocomplete
    }
    run.texts = [v for v in values.values() if isinstance(v, str)]
    interaction = run.discord.interaction(run.role, data=_options_data(command, values))
    run.interaction = interaction
    cog = run.cog(case.cog)

    async def go() -> None:
        tree = command_tree(run.discord)
        for name, current in typed.items():
            autocomplete = command._params[name].autocomplete
            assert autocomplete is not None
            typing = run.discord.interaction(run.role, data={"options": [{"name": name, "value": current}]})
            typing.type = discord.InteractionType.autocomplete
            if not await tree.interaction_check(typing):
                continue
            if getattr(autocomplete, "pass_command_binding", False):
                choices = await autocomplete(cog, typing, current)
            else:
                choices = await autocomplete(typing, current)
            run.discord.sent.append(Sent(where="autocomplete", method="autocomplete", choices=list(choices)))

        if not await tree.interaction_check(interaction) or not await command._check_can_run(interaction):
            run.refused = True
            return
        kwargs = {}
        for name, value in values.items():
            try:
                kwargs[name] = await command._params[name].transform(interaction, value)
            except app_commands.AppCommandError as error:
                # discord.py hands a transformer's error to the tree, which answers it
                await tree.on_error(interaction, error)
                run.refused = True
                return
        await command.callback(cog, interaction, **kwargs)

    return go


def _find_item(view: discord.ui.View, target: Any) -> discord.ui.Item[Any]:
    """The item of this class, or the one a @discord.ui.button method of the view makes."""
    for item in view.children:
        if isinstance(target, type) and isinstance(item, target):
            return item
        if getattr(item.callback, "callback", None) is target:
            return item
    raise AssertionError(f"no {target} on {type(view).__name__}")


def prepare_item(run: Run, case: Item, data: Draws) -> Go:
    message = case.message(run, data) if case.message else None
    interaction = run.discord.interaction(run.role, message=message, component=True)
    run.interaction = interaction
    view = case.view(run, data)
    item = _find_item(view, case.target)
    if isinstance(item, discord.ui.Select):
        values = [o.value for o in item.options]
        item._values = [data.draw(st.sampled_from(values), label="chosen")] if values else []

    async def go() -> None:
        if not await view.interaction_check(interaction) or not await item.interaction_check(interaction):
            run.refused = True
            return
        await item.callback(interaction)

    return go


def text_inputs(modal: discord.ui.Modal) -> list[discord.ui.TextInput[Any]]:
    return [item for item in modal.children if isinstance(item, discord.ui.TextInput)]


def input_label(text_input: discord.ui.TextInput[Any]) -> str:
    return str(text_input.to_component_dict().get("label") or "")


def prepare_modal(run: Run, case: Modal, data: Draws) -> Go:
    modal = case.build(run, data)
    for text_input in text_inputs(modal):
        # Discord enforces a text input's limits, and sends a required one filled
        low = max(text_input.min_length or 0, 1 if text_input.required else 0)
        high = text_input.max_length or 4000
        label = input_label(text_input)
        hint = case.hints.get(label)
        strategy = option_text(low, high)
        if hint is not None:
            strategy = st.one_of(hint(run.world).filter(lambda s, lo=low, hi=high: lo <= len(s) <= hi), strategy)
        text_input._value = data.draw(strategy, label=label)
        run.texts.append(text_input.value)
    interaction = run.discord.interaction(run.role)
    run.interaction = interaction

    async def go() -> None:
        if not await modal.interaction_check(interaction):
            run.refused = True
            return
        await modal.on_submit(interaction)

    return go


# A listener's message


@dataclass
class Attachment:
    filename: str
    content_type: str | None
    size: int
    data: bytes

    async def read(self) -> bytes:
        return self.data


def attachments() -> st.SearchStrategy[list[Attachment]]:
    one = st.builds(
        lambda name, ctype, data, big: Attachment(name, ctype, 10 * 1024 * 1024 + 1 if big else len(data), data),
        file_names(),
        st.one_of(st.none(), request_text(100)),
        file_bytes(),
        st.booleans(),
    )
    return st.lists(one, max_size=3)


def dashboard_view(run: Run, data: Draws) -> discord.ui.View:
    """The dashboard's buttons, on a dashboard whose message is in the ticket queue channel."""
    from bot.unified_dashboard import DashboardControlView, UnifiedDashboard

    dashboard = UnifiedDashboard(run.discord.bot)
    dashboard.dashboard_message_id = 1
    dashboard.dashboard_channel_id = QUEUE_CHANNEL_ID
    return DashboardControlView(dashboard)


def channel_for(run: Run, data: Draws) -> Any:
    """Where a message is posted: a ticket's thread, a thread no ticket has, the link channel or a team chat."""
    choice = data.draw(st.sampled_from(("ticket thread", "other thread", "link", "chat")), label="channel")
    if choice == "ticket thread":
        return run.discord.channel(data.draw(st.sampled_from(run.world.thread_ids), label="thread"))
    if choice == "other thread":
        return run.discord.thread(next(run.discord._next_id))
    return run.discord.channel(LINK_CHANNEL_ID if choice == "link" else TEAM_CHAT_ID)


def message_for(run: Run, data: Draws, *, channel: Any = None, message_id: int | None = None) -> Any:
    author = run.discord.member_for(run.role)
    author.bot = data.draw(st.sampled_from((False, False, False, True)), label="author is a bot")
    message = run.discord.message_object(channel or channel_for(run, data))
    if message_id is not None:
        message.id = message_id
    message.author = author
    message.content = data.draw(option_text(0, 4000), label="content")
    message.attachments = data.draw(attachments(), label="attachments")
    run.texts += [message.content, *(a.filename for a in message.attachments)]
    return message


def on_message(run: Run, data: Draws, cog: Any) -> Go:
    message = message_for(run, data)

    async def go() -> None:
        await cog.on_message(message)

    return go


def saved_message(run: Run, data: Draws) -> tuple[Any, int]:
    """A channel and a message id, the message saved as a ticket comment when the channel is a ticket's thread."""
    from ticketing.models import TicketComment

    channel = channel_for(run, data)
    ticket = Ticket.objects.filter(discord_thread_id=channel.id).first()
    message_id = data.draw(st.integers(1, 2**63 - 1), label="message id")
    if ticket and data.draw(st.booleans(), label="was saved"):
        TicketComment.objects.create(ticket=ticket, comment_text="before", discord_message_id=message_id)
    return channel, message_id


def on_message_edit(run: Run, data: Draws, cog: Any) -> Go:
    channel, message_id = saved_message(run, data)
    before = message_for(run, data, channel=channel, message_id=message_id)
    after = message_for(run, data, channel=channel, message_id=message_id)
    after.author = before.author

    async def go() -> None:
        await cog.on_message_edit(before, after)

    return go


def on_message_delete(run: Run, data: Draws, cog: Any) -> Go:
    channel, message_id = saved_message(run, data)
    message = message_for(run, data, channel=channel, message_id=message_id)

    async def go() -> None:
        await cog.on_message_delete(message)

    return go


# ---------------------------------------------------------------------------------------------------------------
# The properties
# ---------------------------------------------------------------------------------------------------------------

MESSAGE_LIMIT = 2000
EMBED_LIMITS = {"title": 256, "description": 4096, "footer": 2048, "author": 256}
EMBED_TOTAL = 6000
FIELD_NAME, FIELD_VALUE, FIELDS = 256, 1024, 25


def embed_problems(embeds: list[discord.Embed]) -> list[str]:
    problems = []
    total = 0
    if len(embeds) > 10:
        problems.append(f"{len(embeds)} embeds (Discord takes 10)")
    for embed in embeds:
        data = embed.to_dict()
        title = data.get("title", "")
        description = data.get("description", "")
        footer = data.get("footer", {}).get("text", "")
        author = data.get("author", {}).get("name", "")
        for name, text in (("title", title), ("description", description), ("footer", footer), ("author", author)):
            if len(text) > EMBED_LIMITS[name]:
                problems.append(f"embed {name} of {len(text)} characters (Discord takes {EMBED_LIMITS[name]})")
        fields = data.get("fields", [])
        if len(fields) > FIELDS:
            problems.append(f"{len(fields)} embed fields (Discord takes {FIELDS})")
        for f in fields:
            if not str(f["name"]).strip() or not str(f["value"]).strip():
                problems.append(f"a blank embed field {f['name'][:40]!r}: {f['value'][:40]!r}")
            if len(f["name"]) > FIELD_NAME:
                problems.append(f"an embed field name of {len(f['name'])} characters (Discord takes {FIELD_NAME})")
            if len(f["value"]) > FIELD_VALUE:
                problems.append(f"an embed field value of {len(f['value'])} characters (Discord takes {FIELD_VALUE})")
        total += len(title) + len(description) + len(footer) + len(author)
        total += sum(len(f["name"]) + len(f["value"]) for f in fields)
    if total > EMBED_TOTAL:
        problems.append(f"embeds of {total} characters in all (Discord takes {EMBED_TOTAL})")
    return problems


def view_problems(view: discord.ui.View) -> list[str]:
    problems = []
    if len(view.children) > 25:
        problems.append(f"a view with {len(view.children)} items")
    for item in view.children:
        if isinstance(item, discord.ui.Button):
            if item.label is not None and len(item.label) > 80:
                problems.append(f"a button label of {len(item.label)} characters (Discord takes 80)")
        elif isinstance(item, discord.ui.Select):
            if not 1 <= len(item.options) <= 25:
                problems.append(f"a select with {len(item.options)} options (Discord takes 1 to 25)")
            if item.placeholder and len(item.placeholder) > 150:
                problems.append(f"a select placeholder of {len(item.placeholder)} characters")
            for option in item.options:
                if not 1 <= len(option.label) <= 100 or len(option.value) > 100:
                    problems.append(f"a select option {option.label[:40]!r} longer than Discord takes (100)")
                if option.description and len(option.description) > 100:
                    problems.append(f"a select option description of {len(option.description)} characters")
    return problems


def modal_problems(modal: discord.ui.Modal) -> list[str]:
    problems = []
    if not 1 <= len(str(modal.title)) <= 45:
        problems.append(f"a modal title of {len(str(modal.title))} characters (Discord takes 45)")
    inputs = text_inputs(modal)
    if len(inputs) > 5:
        problems.append(f"a modal with {len(inputs)} text inputs (Discord takes 5)")
    for text_input in inputs:
        label = input_label(text_input)
        if not 1 <= len(label) <= 45:
            problems.append(f"a text input label of {len(label)} characters (Discord takes 45)")
        if text_input.placeholder and len(text_input.placeholder) > 100:
            problems.append(f"a text input placeholder of {len(text_input.placeholder)} characters (Discord takes 100)")
        if text_input.max_length is not None and not 1 <= text_input.max_length <= 4000:
            problems.append(f"a text input max_length of {text_input.max_length} (Discord takes 1 to 4000)")
    return problems


def sent_problems(sent: Sent) -> list[str]:
    """What in one sent thing Discord would refuse."""
    problems = []
    if sent.content is not None and len(str(sent.content)) > MESSAGE_LIMIT:
        problems.append(f"{len(str(sent.content))} characters of content (Discord takes {MESSAGE_LIMIT})")
    if sent.method in ("send", "send_message", "followup.send") and not (
        (sent.content and str(sent.content).strip()) or sent.embeds or sent.files
    ):
        problems.append("an empty message")
    problems += embed_problems(sent.embeds)
    if sent.view is not None:
        problems += view_problems(sent.view)
    if sent.modal is not None:
        problems += modal_problems(sent.modal)
    if sent.method == "create_thread" and not 1 <= len(sent.name or "") <= 100:
        problems.append(f"a thread name of {len(sent.name or '')} characters (Discord takes 1 to 100)")
    if sent.choices is not None:
        if len(sent.choices) > 25:
            problems.append(f"{len(sent.choices)} autocomplete choices (Discord takes 25)")
        for choice in sent.choices:
            if not 1 <= len(choice.name) <= 100 or (isinstance(choice.value, str) and len(choice.value) > 100):
                problems.append(f"an autocomplete choice {choice.name[:40]!r} longer than Discord takes (100)")
    return problems


def mention_problems(sent: Sent, default: discord.AllowedMentions | None) -> list[str]:
    """A message whose mentions aren't limited to the ones the bot lists: user text in it could ping anyone. The
    send's allowed mentions are merged over the client's default, as discord.py does (no default pings all)."""
    if sent.content is None:
        return []
    base = default or discord.AllowedMentions.all()
    allowed = base.merge(sent.allowed_mentions) if sent.allowed_mentions else base
    problems = []
    if allowed.everyone:
        problems.append("@everyone and @here")
    if allowed.users is True:
        problems.append("every user named")
    if allowed.roles is True:
        problems.append("every role named")
    return problems


def check_run(case: BotCase, run: Run, writes_before: dict[str, Any], *, answers: bool) -> None:
    where = f"{case.name} as {run.role}"
    for sent in run.discord.sent:
        problems = sent_problems(sent)
        assert not problems, f"{where}: {sent.method} to {sent.where} Discord would refuse: {problems}"
        pings = mention_problems(sent, run.discord.bot.allowed_mentions)
        assert not pings, f"{where}: {sent.method} to {sent.where} may ping {pings}: {str(sent.content)[:200]!r}"
    if answers and run.interaction is not None:
        assert run.discord.answered(run.interaction), f"{where}: the interaction was never answered"
    run.written = sorted(name for name, counts in table_writes().items() if writes_before.get(name) != counts)
    if run.refused or run.role not in case.allowed:
        assert not run.written, f"{where} was refused but wrote {run.written}"


def drive(case: BotCase, world: World, data: Draws) -> Run:
    """One example of a row: a role, the inputs, the handler run, the properties checked; returns the run (what was
    sent, and the tables written, rolled back by now). Everything is drawn before the handler runs: Hypothesis draws
    only on the test's thread, and the handler runs on async_to_sync's event loop (its ORM calls come back to the
    test's thread, and its transaction)."""
    reached_by = getattr(case, "reached_by", EVERYONE)
    role = data.draw(st.sampled_from(sorted(reached_by)), label="role")
    confirm = data.draw(st.sampled_from((True, False, None)), label="confirmation")
    with rolled_back(), discord_world(world, confirm) as discord_:
        seed_hostile(world, data)
        _load_cogs(discord_)
        run = Run(world=world, discord=discord_, role=role)
        match case:
            case Command():
                go = prepare_command(run, case, data)
            case Item():
                go = prepare_item(run, case, data)
            case Modal():
                go = prepare_modal(run, case, data)
            case Listener():
                go = case.prepare(run, data, run.cog(case.cog))
        before = table_writes()
        async_to_sync(go)()
        check_run(case, run, before, answers=not isinstance(case, Listener))
        return run


class Chosen:
    """Stands in for Hypothesis's data in drive: each draw returns the value chosen for its label, so a regression
    test runs one input of a row (the label of an option, a text input or a draw in a row's helper). A label not
    chosen takes a plain default (an optional option not chosen is omitted, nothing typed, the plain world, a
    confirmation given, a person's message with no attachments); any other label must be chosen."""

    DEFAULTS: dict[str, Any] = {  # noqa: RUF012 - read only
        "hostile rows": False,
        "confirmation": True,
        "author is a bot": False,
        "attachments": [],
        "content": "",
        "view ticket": None,
    }

    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def draw(self, strategy: st.SearchStrategy[Any], label: str | None = None) -> Any:
        if label in self.values:
            return self.values[label]
        if label and label.startswith("omit "):
            return label.removeprefix("omit ") not in self.values
        if label and label.startswith("typing "):
            return ""
        if label in self.DEFAULTS:
            return self.DEFAULTS[label]
        raise KeyError(f"choose a value for {label!r}")


def let_in(case: Command, world: World, role: str) -> bool:
    """Whether the command's checks let the role in (with no input the command tree refuses)."""
    with discord_world(world, None) as discord_:
        _load_cogs(discord_)
        command = bound_command(Run(world=world, discord=discord_, role=role), case)
        return async_to_sync(command._check_can_run)(discord_.interaction(role))
