"""Unified dashboard for #ticket-queue channel showing all tickets."""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils import timezone

from bot.heartbeat import record as record_heartbeat
from bot.utils import DISCORD_EMBED_FIELD_CHAR_LIMIT, recycle_db_connection
from core.models import BotState
from core.tickets_config import get_category_config
from ticketing.models import Ticket

logger = logging.getLogger(__name__)

DASHBOARD_DEBOUNCE_SECONDS = 10


class UnifiedDashboard:
    """Manages a single dashboard message showing all tickets."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.running = False
        self.task: asyncio.Task[None] | None = None
        self.dashboard_message_id: int | None = None
        self.dashboard_channel_id: int | None = None
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

        await self._initialize_dashboard()

        while self.running:
            try:
                await recycle_db_connection()
                await self._check_and_update()
                record_heartbeat("dashboard", self.bot)
            except Exception as e:
                logger.exception(f"Error in dashboard loop: {e}")

            await asyncio.sleep(DASHBOARD_DEBOUNCE_SECONDS)

    async def _initialize_dashboard(self) -> None:
        """Initialize or reconnect to existing dashboard message."""
        queue_channel_id = settings.DISCORD_TICKET_QUEUE_CHANNEL_ID
        if not queue_channel_id:
            logger.warning("DISCORD_TICKET_QUEUE_CHANNEL_ID not configured")
            return

        @sync_to_async
        def get_dashboard_state() -> tuple[int | None, int | None]:
            try:
                msg_state = BotState.objects.get(key="unified_dashboard_message_id")
                chan_state = BotState.objects.get(key="unified_dashboard_channel_id")
                return int(msg_state.value), int(chan_state.value)
            except BotState.DoesNotExist:
                return None, None

        msg_id, chan_id = await get_dashboard_state()

        if msg_id and chan_id:
            try:
                channel = self.bot.get_channel(chan_id)
                if channel and isinstance(channel, discord.TextChannel):
                    message = await channel.fetch_message(msg_id)
                    self.dashboard_message_id = msg_id
                    self.dashboard_channel_id = chan_id
                    logger.info(f"Reconnected to existing dashboard message {msg_id}")

                    await self._update_dashboard()
                    return
            except Exception as e:
                logger.warning(f"Could not reconnect to existing dashboard: {e}")

        channel = self.bot.get_channel(queue_channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            logger.error(f"Could not find text channel {queue_channel_id}")
            return

        embed = discord.Embed(
            title="📋 Ticket Queue Dashboard",
            description="Loading tickets...",
            color=discord.Color.blue(),
        )

        view = DashboardControlView(self)
        message = await channel.send(embed=embed, view=view)

        self.dashboard_message_id = message.id
        self.dashboard_channel_id = channel.id

        @sync_to_async
        def save_dashboard_state() -> None:
            BotState.objects.update_or_create(key="unified_dashboard_message_id", defaults={"value": str(message.id)})
            BotState.objects.update_or_create(key="unified_dashboard_channel_id", defaults={"value": str(channel.id)})

        await save_dashboard_state()
        logger.info(f"Created new dashboard message {message.id}")

        await self._update_dashboard()

    async def _check_and_update(self) -> None:
        if not self._needs_update:
            return
        # Cleared first so a trigger during the refresh isn't lost; a refresh that fails on the DB is
        # retried next pass, and the error propagates so the loop skips its heartbeat.
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
            return " ⛔"
        if time_since_claim > timedelta(hours=1):
            return " 🚨"
        if time_since_claim > timedelta(minutes=30):
            return " ⚠️"
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
        self, category_name: str, tickets: list[Ticket], guild_id: int, playbook_url: str = ""
    ) -> tuple[str, str]:
        """Build the (name, value) embed field for one ticket category."""
        lines = []
        if playbook_url:
            lines.append(f"📖 [Playbook]({playbook_url})")
        for ticket in tickets:
            if ticket.status == "open":
                status_emoji = "🔴"
            elif ticket.status == "claimed":
                status_emoji = "🟡"
            else:
                status_emoji = "🔵"

            stale = self._get_stale_indicator(ticket)

            if ticket.discord_thread_id:
                thread_link = f"https://discord.com/channels/{guild_id}/{ticket.discord_thread_id}"
                ticket_display = f"[{ticket.ticket_number}]({thread_link})"
            else:
                ticket_display = f"**{ticket.ticket_number}**"

            time_str = self._get_time_ago(ticket.created_at)
            desc_preview = ticket.description[:40] + "..." if len(ticket.description) > 40 else ticket.description

            assignee = ""
            if ticket.assigned_to:
                assignee = f" - {ticket.assigned_to.username}"

            lines.append(
                f"{status_emoji} {ticket_display} {ticket.team.team_name}{assignee}{stale}\n"
                f"   ↳ *{desc_preview}* ({time_str})"
            )

        field_value = "\n".join(lines) if lines else "No tickets"
        if len(field_value) > DISCORD_EMBED_FIELD_CHAR_LIMIT:
            field_value = field_value[: DISCORD_EMBED_FIELD_CHAR_LIMIT - 4] + "..."

        return f"{category_name} ({len(tickets)})", field_value

    async def _update_dashboard(self) -> None:
        if not self.dashboard_message_id or not self.dashboard_channel_id:
            return

        try:
            channel = self.bot.get_channel(self.dashboard_channel_id)
            if not channel or not isinstance(channel, discord.TextChannel):
                logger.error(f"Dashboard channel {self.dashboard_channel_id} not found")
                return

            message = await channel.fetch_message(self.dashboard_message_id)

            @sync_to_async
            def get_tickets() -> list[Ticket]:
                if self.filter_status == "all":
                    query = Ticket.objects.filter(status__in=["open", "claimed"])
                else:
                    query = Ticket.objects.filter(status=self.filter_status)
                return list(query.select_related("team", "assigned_to").order_by("created_at"))

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
                title="📋 Ticket Queue Dashboard",
                description=(
                    f"**{len(tickets)} active tickets** "
                    f"(Sort: {self.sort_by.title()} | Filter: {self.filter_status.replace('_', ' ').title()})"
                ),
                color=discord.Color.blue(),
                timestamp=timezone.now(),
            )

            if not tickets:
                resolved_today, avg_time = await get_stats()
                embed.description = "✅ **No active tickets!**"
                embed.add_field(
                    name="📊 Today's Stats",
                    value=f"**{resolved_today}** tickets resolved\n**{avg_time}** min avg resolution time",
                    inline=False,
                )
            else:
                categorized = self._categorize_tickets(tickets)
                guild_id = channel.guild.id
                for category_id, cat_tickets in categorized.items():
                    cat_info = await sync_to_async(get_category_config)(category_id) or {
                        "display_name": f"Category {category_id}"
                    }
                    field_name, field_value = self._build_category_field(
                        cat_info["display_name"], cat_tickets, guild_id, cat_info.get("playbook_url", "")
                    )
                    embed.add_field(name=field_name, value=field_value, inline=False)

            embed.set_footer(text="🔴 Open | 🟡 Claimed (Working) | ⚠️ >30min | 🚨 >1hr | ⛔ >2hr")

            view = DashboardControlView(self)
            await message.edit(embed=embed, view=view)
            logger.info("Updated unified dashboard")

        except discord.NotFound:
            self.dashboard_message_id = None
            self.dashboard_channel_id = None
        except discord.HTTPException as e:
            # Discord trouble is not ours to fix by restarting; anything else (the DB) propagates.
            logger.exception(f"Error updating dashboard: {e}")

    def trigger_update(self) -> None:
        """Refresh the dashboard on the loop's next pass, so a burst of ticket changes costs one edit."""
        self._needs_update = True


class DashboardControlView(discord.ui.View):
    """Control buttons for dashboard sorting and filtering."""

    def __init__(self, dashboard: UnifiedDashboard) -> None:
        super().__init__(timeout=None)
        self.dashboard = dashboard

    @discord.ui.button(
        label="Sort: Created",
        style=discord.ButtonStyle.secondary,
        custom_id="sort_created",
        row=0,
    )
    async def sort_created(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        self.dashboard.sort_by = "created"
        await self.dashboard._update_dashboard()
        await interaction.response.send_message("Sorted by creation time", ephemeral=True)

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
        self.dashboard.sort_by = "stale"
        await self.dashboard._update_dashboard()
        await interaction.response.send_message("Sorted by stale (oldest assigned first)", ephemeral=True)

    @discord.ui.button(
        label="Sort: Team",
        style=discord.ButtonStyle.secondary,
        custom_id="sort_team",
        row=0,
    )
    async def sort_team(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        self.dashboard.sort_by = "team"
        await self.dashboard._update_dashboard()
        await interaction.response.send_message("Sorted by team name", ephemeral=True)

    @discord.ui.button(
        label="Filter: All",
        style=discord.ButtonStyle.primary,
        custom_id="filter_all",
        row=1,
    )
    async def filter_all(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        self.dashboard.filter_status = "all"
        await self.dashboard._update_dashboard()
        await interaction.response.send_message("Showing all active tickets", ephemeral=True)

    @discord.ui.button(
        label="Filter: Open",
        style=discord.ButtonStyle.primary,
        custom_id="filter_open",
        row=1,
    )
    async def filter_open(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        self.dashboard.filter_status = "open"
        await self.dashboard._update_dashboard()
        await interaction.response.send_message("Showing only open tickets", ephemeral=True)

    @discord.ui.button(
        label="Filter: Claimed",
        style=discord.ButtonStyle.primary,
        custom_id="filter_claimed",
        row=1,
    )
    async def filter_claimed(
        self, interaction: discord.Interaction, button: discord.ui.Button[DashboardControlView]
    ) -> None:
        self.dashboard.filter_status = "claimed"
        await self.dashboard._update_dashboard()
        await interaction.response.send_message("Showing only claimed tickets", ephemeral=True)
