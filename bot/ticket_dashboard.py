"""Ticket dashboard management for #ticket-queue channel."""

import logging

import discord
from asgiref.sync import sync_to_async

from bot.utils import (
    DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT,
    DISCORD_EMBED_FIELD_CHAR_LIMIT,
    DISCORD_EMBED_TITLE_CHAR_LIMIT,
    fit,
)
from core.tickets_config import TicketCategoryConfig, get_category_config
from ticketing.access import TICKET_STAFF
from ticketing.models import Ticket

logger = logging.getLogger(__name__)


def get_ticket_color(status: str) -> discord.Color:
    colors: dict[str, discord.Color] = {
        "open": discord.Color.red(),
        "claimed": discord.Color.orange(),
        "resolved": discord.Color.green(),
        "cancelled": discord.Color.dark_grey(),
    }
    return colors.get(status, discord.Color.default())


def format_ticket_embed(ticket: Ticket) -> discord.Embed:
    cat_info = get_category_config(ticket.category_id) or {}

    embed = discord.Embed(
        title=fit(f"Ticket {ticket.ticket_number}: {ticket.title}", DISCORD_EMBED_TITLE_CHAR_LIMIT),
        description=fit(ticket.description, DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT),
        color=get_ticket_color(ticket.status),
        timestamp=ticket.created_at,
    )

    embed.add_field(
        name="Team",
        value=f"{ticket.team.team_name} (#{ticket.team.team_number})",
        inline=True,
    )

    status_display = ticket.status.replace("_", " ").title()
    embed.add_field(name="Status", value=status_display, inline=True)

    if ticket.hostname:
        embed.add_field(name="Hostname", value=ticket.hostname, inline=True)
    if ticket.service_name:
        embed.add_field(name="Service", value=ticket.service_name, inline=True)
    if ticket.ip_address:
        embed.add_field(name="IP Address", value=ticket.ip_address, inline=True)

    if ticket.assigned_to:
        embed.add_field(
            name="Assigned To",
            value=ticket.assigned_to.username,
            inline=False,
        )

    points = cat_info.get("points", 0)
    if cat_info.get("variable_points", False):
        point_text = "Variable"
        embed.add_field(name="Point Impact", value=point_text, inline=True)
    elif points > 0:
        point_text = f"{points} points"
        embed.add_field(name="Point Impact", value=point_text, inline=True)

    if ticket.resolved_at:
        embed.add_field(
            name="Resolved At",
            value=discord.utils.format_dt(ticket.resolved_at, style="R"),
            inline=True,
        )
        if ticket.resolution_notes:
            embed.add_field(
                name="Resolution Notes",
                value=ticket.resolution_notes[:DISCORD_EMBED_FIELD_CHAR_LIMIT],
                inline=False,
            )

    embed.set_footer(text=f"Category: {cat_info.get('display_name', f'Category {ticket.category_id}')}")

    return embed


def trigger_dashboard(bot: discord.Client) -> None:
    """Refresh the unified ticket dashboard on its next pass (a no-op before it has started)."""
    if hasattr(bot, "unified_dashboard") and bot.unified_dashboard:
        bot.unified_dashboard.trigger_update()


class TicketActionView(discord.ui.View):
    """Action buttons for ticket dashboard."""

    def __init__(self, ticket_id: int, thread_url: str | None = None) -> None:
        super().__init__(timeout=None)
        self.ticket_id = ticket_id

        if thread_url:
            self.add_item(
                discord.ui.Button(
                    label="Go to Thread",
                    style=discord.ButtonStyle.link,
                    url=thread_url,
                    row=0,
                )
            )

    async def _get_ticket_id_from_interaction(self, interaction: discord.Interaction) -> int | None:
        """Extract ticket ID from interaction message or instance variable."""
        import re

        # If instance has ticket_id, use it (for newly created views)
        if hasattr(self, "ticket_id") and self.ticket_id:
            return self.ticket_id

        # Extract from message embed (for persistent views after bot restart)
        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            if embed.title:
                # Title format: "Ticket T050-008: Title"
                match = re.match(r"Ticket ([^:]+):", embed.title)
                if match:
                    ticket_number = match.group(1).strip()
                    ticket = await Ticket.objects.filter(ticket_number=ticket_number).afirst()
                    if ticket:
                        return ticket.id

        return None

    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.primary,
        custom_id="ticket_claim_persistent",
        row=1,
    )
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button[TicketActionView]) -> None:
        from bot.permissions import has_permission

        ticket_id = await self._get_ticket_id_from_interaction(interaction)
        if not ticket_id:
            await interaction.response.send_message(
                "Could not identify ticket from this message.",
                ephemeral=True,
            )
            return

        if not await has_permission(interaction.user.id, TICKET_STAFF):
            await interaction.response.send_message(
                "You don't have permission to claim tickets. "
                "Contact an administrator if you need ticketing support access.",
                ephemeral=True,
            )
            return

        from ticketing.lifecycle import aclaim_ticket

        ticket, error = await aclaim_ticket(
            ticket_id=ticket_id,
            actor_username=str(interaction.user),
            discord_id=interaction.user.id,
            discord_username=str(interaction.user),
        )

        if error or ticket is None:
            await interaction.response.send_message(error or "Failed to claim ticket.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"You have claimed ticket {ticket.ticket_number}.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Resolve",
        style=discord.ButtonStyle.success,
        custom_id="ticket_resolve_persistent",
        row=1,
    )
    async def resolve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[TicketActionView]
    ) -> None:
        """Show resolve modal with category dropdown and notes."""
        from bot.permissions import has_permission

        ticket_id = await self._get_ticket_id_from_interaction(interaction)
        if not ticket_id:
            await interaction.response.send_message(
                "Could not identify ticket from this message.",
                ephemeral=True,
            )
            return

        if not await has_permission(interaction.user.id, TICKET_STAFF):
            await interaction.response.send_message(
                "You don't have permission to resolve tickets. "
                "Contact an administrator if you need ticketing support access.",
                ephemeral=True,
            )
            return

        ticket = await Ticket.objects.select_related("assigned_to").filter(id=ticket_id).afirst()
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        from ticketing.lifecycle import assignee_refusal, refusal
        from ticketing.utils import get_user_for_ticket

        # Asked before the modal so no one fills it in for nothing; the lifecycle asks again under its lock
        actor = await sync_to_async(get_user_for_ticket)(discord_id=interaction.user.id)
        if error := refusal(ticket, "resolve") or await sync_to_async(assignee_refusal)(ticket, actor, "resolve"):
            await interaction.response.send_message(error, ephemeral=True)
            return

        cat_info = await sync_to_async(get_category_config)(ticket.category_id) or {}
        modal = ResolveTicketModal(ticket, cat_info)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        custom_id="ticket_cancel_persistent",
        row=2,
    )
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[TicketActionView]
    ) -> None:
        """Cancel a ticket: teams their own open ones, ticketing staff claimed ones too."""

        from bot.permissions import has_permission, linked_team_member

        ticket_id = await self._get_ticket_id_from_interaction(interaction)
        if not ticket_id:
            await interaction.response.send_message(
                "Could not identify ticket from this message.",
                ephemeral=True,
            )
            return

        is_ops = await has_permission(interaction.user.id, TICKET_STAFF)
        member = await linked_team_member(interaction.user.id)

        if not is_ops and not member:
            await interaction.response.send_message(
                "You must be a team member or ops to cancel tickets.",
                ephemeral=True,
            )
            return

        ticket = await Ticket.objects.select_related("team").filter(id=ticket_id).afirst()
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        # If team member (not ops), verify ticket belongs to their team
        if not is_ops and member and ticket.team_id != member.team.id:
            await interaction.response.send_message("This ticket does not belong to your team.", ephemeral=True)
            return

        from ticketing.lifecycle import acancel_ticket

        cancelled, error = await acancel_ticket(ticket_id=ticket.id, actor_username=str(interaction.user), staff=is_ops)
        if error or cancelled is None:
            await interaction.response.send_message(error or "Failed to cancel ticket.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Ticket {cancelled.ticket_number} has been cancelled (no point penalty).",
            ephemeral=True,
        )


class ResolveTicketModal(discord.ui.Modal, title="Resolve Ticket"):
    def __init__(self, ticket: Ticket, cat_info: TicketCategoryConfig) -> None:
        super().__init__()
        self.ticket = ticket

        self.notes: discord.ui.TextInput[ResolveTicketModal] = discord.ui.TextInput(
            label="Resolution Notes",
            placeholder="Describe how the issue was resolved...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )

        self.points: discord.ui.TextInput[ResolveTicketModal]
        if cat_info.get("variable_points", False):
            min_pts = cat_info.get("min_points", 0)
            max_pts = cat_info.get("max_points", 0)
            placeholder = f"Enter points ({min_pts}-{max_pts})" if max_pts else f"Enter points (min {min_pts})"
            self.points = discord.ui.TextInput(
                label="Points",
                placeholder=placeholder,
                required=True,
                max_length=5,
            )
        else:
            # Show fixed points that will be charged if left blank
            fixed_pts = cat_info.get("points", 0)
            self.points = discord.ui.TextInput(
                label="Points Override",
                placeholder=f"Leave blank for default ({fixed_pts}pt) or enter to override",
                required=False,
                max_length=5,
            )

        self.add_item(self.notes)
        self.add_item(self.points)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from bot.ticket_input import read_resolution
        from ticketing.lifecycle import aresolve_ticket

        resolution = read_resolution(self.notes.value, self.points.value)
        if isinstance(resolution, str):
            await interaction.response.send_message(resolution, ephemeral=True)
            return

        ticket, error = await aresolve_ticket(
            ticket_id=self.ticket.id,
            actor_username=str(interaction.user),
            resolution_notes=resolution[0],
            points_override=resolution[1],
            discord_id=interaction.user.id,
            discord_username=str(interaction.user),
        )

        if error or ticket is None:
            await interaction.response.send_message(error or "Failed to resolve ticket.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Ticket {ticket.ticket_number} resolved with {ticket.points_charged} point penalty.",
            ephemeral=True,
        )
