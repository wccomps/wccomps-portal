"""Unified dashboard for #ticket-queue channel showing all tickets."""

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta

import aiohttp
import discord
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from bot.heartbeat import record as record_heartbeat
from bot.permissions import has_permission
from bot.utils import (
    DISCORD_EMBED_FIELD_CHAR_LIMIT,
    add_field,
    fit,
    fit_lines,
    log_to_ops_channel,
    recycle_db_connection,
)
from core.models import BotState
from core.utils import shorten
from ticketing.models import Ticket

logger = logging.getLogger(__name__)

DASHBOARD_DEBOUNCE_SECONDS = 10
DASHBOARD_FOOTER_ROOM = 100  # kept for the footer counting the categories that don't fit
DASHBOARD_MESSAGE_KEY = "unified_dashboard_message_id"
# Discord being unreachable or refusing; restarting the bot fixes none of these
# Discord's code for a request it rejects as malformed: the embed is at fault, not the message
INVALID_FORM_BODY = 50035
DISCORD_TROUBLE = (discord.HTTPException, aiohttp.ClientError, TimeoutError)


class UnifiedDashboard:
    """Manages a single dashboard message showing all tickets."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.running = False
        self.task: asyncio.Task[None] | None = None
        self.message: discord.PartialMessage | None = None
        self._reported_problems: set[tuple[str, int | None, int | None]] = set()
        self.sort_by = "created"  # Options: created, stale, team
        self.filter_status = "all"  # Options: all, open, claimed
        self._needs_update = False

    def start(self) -> None:
        self.running = True
        self.task = asyncio.create_task(self._dashboard_loop())
        logger.info("Unified dashboard started")

    def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
        logger.info("Unified dashboard stopped")

    async def _dashboard_loop(self) -> None:
        """Main dashboard loop - checks for updates every 10 seconds."""
        await self.bot.wait_until_ready()

        while self.running:
            try:
                await recycle_db_connection()
                await self._pass()
                record_heartbeat("dashboard", self.bot)
            except Exception as e:
                logger.exception(f"Error in dashboard loop: {e}")

            await asyncio.sleep(DASHBOARD_DEBOUNCE_SECONDS)

    async def _pass(self) -> None:
        """Post the dashboard if the bot has none, then refresh it if asked. A Discord failure is reported to the ops
        channel once per cause until a pass succeeds, and the pass still counts; database errors propagate."""
        channel_id = settings.DISCORD_TICKET_QUEUE_CHANNEL_ID
        if not channel_id:
            return
        try:
            if self.message is None:
                await self._initialize_dashboard(channel_id)
            await self._check_and_update()
        except DISCORD_TROUBLE as e:
            await self._report_problem(e, channel_id)
        else:
            self._reported_problems.clear()

    async def _report_problem(self, error: Exception, channel_id: int) -> None:
        logger.warning(f"Ticket queue dashboard in channel {channel_id}: {type(error).__name__}: {error}")
        # Not str(error): a 5xx body differs on every response (Cloudflare puts a Ray ID in it)
        cause = (type(error).__name__, getattr(error, "status", None), getattr(error, "code", None))
        if cause in self._reported_problems:
            return
        self._reported_problems.add(cause)
        await log_to_ops_channel(
            self.bot,
            f"The ticket queue dashboard in <#{channel_id}> can't be posted or updated: "
            f"{fit(f'{type(error).__name__}: {error}', 300)}",
        )

    async def _initialize_dashboard(self, channel_id: int) -> None:
        """Point self.message at the stored dashboard if it can still be edited, else post and store a new one; on
        other Discord trouble it keeps it and raises."""
        channel = self.bot.get_partial_messageable(channel_id, guild_id=settings.COMPETITION_GUILD_ID)

        stored = await BotState.objects.filter(key=DASHBOARD_MESSAGE_KEY).values_list("value", flat=True).afirst()
        if stored and stored.isdigit():
            self.message = channel.get_partial_message(int(stored))
            try:
                await self._update_dashboard()
            except DISCORD_TROUBLE:
                self._needs_update = True
                raise
            if self.message:
                logger.info(f"Reconnected to existing dashboard message {stored}")
                return

        sent = await channel.send(embed=await self._render(), view=DashboardControlView(self))
        self.message = channel.get_partial_message(sent.id)
        await BotState.objects.aupdate_or_create(key=DASHBOARD_MESSAGE_KEY, defaults={"value": str(sent.id)})
        logger.info(f"Created new dashboard message {sent.id}")

    async def _check_and_update(self) -> None:
        if not self._needs_update:
            return
        # Cleared first so a trigger during the refresh isn't lost; a refresh that fails is retried next pass.
        self._needs_update = False
        try:
            await self._update_dashboard()
        except Exception:
            self._needs_update = True
            raise

    def _get_stale_indicator(self, ticket: Ticket) -> str:
        """Get progressive stale indicator based on time claimed."""
        if not ticket.assigned_at:
            return ""

        time_since_claim = timezone.now() - ticket.assigned_at
        if time_since_claim > timedelta(hours=2):
            return " (claimed >2h)"
        if time_since_claim > timedelta(hours=1):
            return " (claimed >1h)"
        if time_since_claim > timedelta(minutes=30):
            return " (claimed >30m)"
        return ""

    def _get_time_ago(self, dt: datetime | None) -> str:
        if not dt:
            return ""

        delta = timezone.now() - dt
        if delta < timedelta(minutes=1):
            return "just now"
        if delta < timedelta(hours=1):
            mins = int(delta.total_seconds() / 60)
            return f"{mins}m ago"
        if delta < timedelta(days=1):
            hours = int(delta.total_seconds() / 3600)
            return f"{hours}h ago"
        days = delta.days
        return f"{days}d ago"

    def _categorize_tickets(self, tickets: list[Ticket]) -> dict[int | None, list[Ticket]]:
        """Group tickets by category_id, highest-count categories first."""
        tickets_by_category: dict[int | None, list[Ticket]] = {}
        for ticket in tickets:
            if ticket.category_id not in tickets_by_category:
                tickets_by_category[ticket.category_id] = []
            tickets_by_category[ticket.category_id].append(ticket)

        sorted_keys = sorted(
            tickets_by_category.keys(),
            key=lambda cat: len(tickets_by_category[cat]),
            reverse=True,
        )
        return {k: tickets_by_category[k] for k in sorted_keys}

    def _build_category_field(
        self,
        category_name: str,
        tickets: list[Ticket],
        guild_id: int,
        playbook_steps: list[str] | None = None,
    ) -> tuple[str, str]:
        """Build the (name, value) embed field for one ticket category."""
        lines = []
        if playbook_steps:
            lines.append("**Playbook:**")
            for idx, step in enumerate(playbook_steps[:3], 1):
                lines.append(f"{idx}. {shorten(step, 80)}")
            if len(playbook_steps) > 3:
                lines.append(f"*(+{len(playbook_steps) - 3} more)*")
        for ticket in tickets:
            stale = self._get_stale_indicator(ticket)

            if ticket.discord_thread_id:
                thread_link = f"https://discord.com/channels/{guild_id}/{ticket.discord_thread_id}"
                ticket_display = f"[{ticket.ticket_number}]({thread_link})"
            else:
                ticket_display = f"**{ticket.ticket_number}**"

            time_str = self._get_time_ago(ticket.created_at)
            desc_preview = shorten(ticket.description, 40)

            assignee = ""
            if ticket.assigned_to:
                assignee = f", {ticket.assigned_to.username}"

            lines.append(
                f"`{ticket.status.upper()}` {ticket_display} {ticket.team.team_name}{assignee}{stale}\n"
                f"   *{desc_preview}* ({time_str})"
            )

        return f"{category_name} ({len(tickets)})", fit_lines(lines, DISCORD_EMBED_FIELD_CHAR_LIMIT) or "No tickets"

    async def _update_dashboard(self) -> None:
        """Show the tickets now. A deleted message is dropped, and one the bot can't edit (403, or a 400 other than
        our own invalid embed) is deleted first, so a new one is posted; a failed delete raises, so no second
        dashboard with live buttons appears."""
        message = self.message
        if message is None:
            return
        embed = await self._render()
        try:
            await message.edit(embed=embed, view=DashboardControlView(self))
        except discord.NotFound:
            self.message = None
            return
        except discord.HTTPException as e:
            if e.status not in (400, 403) or e.code == INVALID_FORM_BODY:
                raise
            logger.warning(f"Dashboard message {message.id} can't be edited ({e.status}); replacing it")
            with suppress(discord.NotFound):
                await message.delete()
            self.message = None
            return
        logger.info("Updated unified dashboard")

    async def _render(self) -> discord.Embed:
        @sync_to_async
        def get_tickets() -> list[Ticket]:
            if self.filter_status == "all":
                query = Ticket.objects.filter(status__in=["open", "claimed"])
            else:
                query = Ticket.objects.filter(status=self.filter_status)
            return list(query.select_related("team", "assigned_to", "category").order_by("created_at"))

        @sync_to_async
        def get_stats() -> tuple[int, int]:
            today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
            resolved_today = Ticket.objects.filter(resolved_at__gte=today_start).count()

            resolved_with_times = Ticket.objects.filter(
                status="resolved",
                resolved_at__isnull=False,
                created_at__isnull=False,
            )
            total_seconds: float = 0
            count = 0
            for ticket in resolved_with_times[:100]:
                if ticket.resolved_at and ticket.created_at:
                    delta = ticket.resolved_at - ticket.created_at
                    total_seconds += delta.total_seconds()
                    count += 1
            avg_minutes = int(total_seconds / count / 60) if count > 0 else 0
            return resolved_today, avg_minutes

        tickets = await get_tickets()

        if self.sort_by == "stale":
            tickets.sort(key=lambda t: t.assigned_at or timezone.now())
        elif self.sort_by == "team":
            tickets.sort(key=lambda t: t.team.team_name)
        else:
            tickets.sort(key=lambda t: t.created_at)

        embed = discord.Embed(
            title="Ticket Queue Dashboard",
            description=(
                f"**{len(tickets)} active tickets** "
                f"(Sort: {self.sort_by.title()} | Filter: {self.filter_status.replace('_', ' ').title()})"
            ),
            color=discord.Color.blue(),
            timestamp=timezone.now(),
        )

        if not tickets:
            resolved_today, avg_time = await get_stats()
            embed.description = "**No active tickets!**"
            embed.add_field(
                name="Today's Stats",
                value=f"**{resolved_today}** tickets resolved\n**{avg_time}** min avg resolution time",
                inline=False,
            )
        else:
            categorized = list(self._categorize_tickets(tickets).items())
            guild_id = settings.COMPETITION_GUILD_ID
            for n, (category_id, cat_tickets) in enumerate(categorized):
                cat = cat_tickets[0].category
                cat_name = cat.display_name if cat else f"Category {category_id}"
                field_name, field_value = self._build_category_field(
                    cat_name, cat_tickets, guild_id, cat.steps if cat else []
                )
                # The embed holds 25 fields and 6000 characters: the categories past that are counted instead
                if not add_field(embed, field_name, field_value, room=DASHBOARD_FOOTER_ROOM):
                    left = sum(len(rest) for _, rest in categorized[n:])
                    embed.set_footer(text=f"{left} more tickets in other categories: see the portal's ticket list")
                    break
        return embed

    def trigger_update(self) -> None:
        """Refresh the dashboard on the loop's next pass, so a burst of ticket changes costs one edit."""
        self._needs_update = True


class DashboardControlView(discord.ui.View):
    """Control buttons for dashboard sorting and filtering."""

    def __init__(self, dashboard: UnifiedDashboard) -> None:
        super().__init__(timeout=None)
        self.dashboard = dashboard

    async def _apply(
        self,
        interaction: discord.Interaction,
        message: str,
        *,
        sort_by: str | None = None,
        filter_status: str | None = None,
    ) -> None:
        if not await has_permission(interaction.user.id, "ticketing_support"):
            await interaction.response.send_message(
                "You do not have permission to change the ticket queue view.",
                ephemeral=True,
            )
            return
        if sort_by is not None:
            self.dashboard.sort_by = sort_by
        if filter_status is not None:
            self.dashboard.filter_status = filter_status
        await interaction.response.defer(ephemeral=True)
        try:
            await self.dashboard._update_dashboard()
        except DISCORD_TROUBLE as e:
            logger.warning(f"Could not update the dashboard from its buttons: {e}")
            message = "Discord didn't take the dashboard update; it is retried shortly."
            self.dashboard.trigger_update()
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(
        label="Sort: Created",
        style=discord.ButtonStyle.secondary,
        custom_id="sort_created",
        row=0,
    )
    async def sort_created(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        await self._apply(interaction, "Sorted by creation time", sort_by="created")

    @discord.ui.button(
        label="Sort: Stale",
        style=discord.ButtonStyle.secondary,
        custom_id="sort_stale",
        row=0,
    )
    async def sort_stale(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        """Sort by stale (oldest assigned first)."""
        await self._apply(interaction, "Sorted by stale (oldest assigned first)", sort_by="stale")

    @discord.ui.button(
        label="Sort: Team",
        style=discord.ButtonStyle.secondary,
        custom_id="sort_team",
        row=0,
    )
    async def sort_team(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        await self._apply(interaction, "Sorted by team name", sort_by="team")

    @discord.ui.button(
        label="Filter: All",
        style=discord.ButtonStyle.primary,
        custom_id="filter_all",
        row=1,
    )
    async def filter_all(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        await self._apply(interaction, "Showing all active tickets", filter_status="all")

    @discord.ui.button(
        label="Filter: Open",
        style=discord.ButtonStyle.primary,
        custom_id="filter_open",
        row=1,
    )
    async def filter_open(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        await self._apply(interaction, "Showing only open tickets", filter_status="open")

    @discord.ui.button(
        label="Filter: Claimed",
        style=discord.ButtonStyle.primary,
        custom_id="filter_claimed",
        row=1,
    )
    async def filter_claimed(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        await self._apply(interaction, "Showing only claimed tickets", filter_status="claimed")
