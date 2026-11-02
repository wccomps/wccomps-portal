"""Per-ticket embed and action buttons posted in ticket threads."""

import logging

import discord
from asgiref.sync import sync_to_async

from bot.utils import (
    DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT,
    DISCORD_EMBED_TITLE_CHAR_LIMIT,
    DISCORD_EMBED_TOTAL_CHAR_LIMIT,
    add_field,
    fit,
)
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
    """The ticket as its thread shows it. A blank field is left out (Discord refuses one), each is cut to its
    limit, and the description takes what is left of the embed's 6000 characters."""
    cat = ticket.category

    embed = discord.Embed(
        title=fit(f"Ticket {ticket.ticket_number}: {ticket.title}", DISCORD_EMBED_TITLE_CHAR_LIMIT),
        color=get_ticket_color(ticket.status),
        timestamp=ticket.created_at,
    )

    add_field(embed, "Team", f"{ticket.team.team_name} (#{ticket.team.team_number})", inline=True)
    add_field(embed, "Status", ticket.status.replace("_", " ").title(), inline=True)
    add_field(embed, "Hostname", ticket.hostname, inline=True)
    add_field(embed, "Service", ticket.service_name, inline=True)
    add_field(embed, "IP Address", ticket.ip_address or "", inline=True)
    if ticket.assigned_to:
        add_field(embed, "Assigned To", ticket.assigned_to.username)

    points = cat.points if cat else 0
    if cat and cat.variable_points:
        add_field(embed, "Point Impact", "Variable", inline=True)
    elif points > 0:
        add_field(embed, "Point Impact", f"{points} points", inline=True)

    if ticket.resolved_at:
        add_field(embed, "Resolved At", discord.utils.format_dt(ticket.resolved_at, style="R"), inline=True)
        add_field(embed, "Resolution Notes", ticket.resolution_notes)

    cat_name = cat.display_name if cat else f"Category {ticket.category_id}"
    embed.set_footer(text=fit(f"Category: {cat_name}", 2048))
    room = DISCORD_EMBED_TOTAL_CHAR_LIMIT - len(embed)
    embed.description = fit(ticket.description, min(DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT, room)) or None

    return embed


def trigger_dashboard(bot: discord.Client) -> None:
    """Refresh the unified ticket dashboard on its next pass (a no-op before it has started)."""
    if hasattr(bot, "unified_dashboard") and bot.unified_dashboard:
        bot.unified_dashboard.trigger_update()


class TicketActionView(discord.ui.View):
    """Claim, resolve, and cancel buttons on a ticket's thread message."""

    def __init__(self, ticket_id: int) -> None:
        super().__init__(timeout=None)
        self.ticket_id = ticket_id

    async def _get_ticket_id_from_interaction(self, interaction: discord.Interaction) -> int | None:
        """Extract ticket ID from instance variable or thread channel."""
        if hasattr(self, "ticket_id") and self.ticket_id:
            return self.ticket_id

        channel = getattr(interaction, "channel", None)
        channel_id = getattr(channel, "id", None)
        if isinstance(channel_id, int):
            ticket = await Ticket.objects.filter(discord_thread_id=channel_id).afirst()
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

        ticket = await Ticket.objects.select_related("assigned_to", "category").filter(id=ticket_id).afirst()
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

        modal = ResolveTicketModal(ticket)
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

        from bot.permissions import ticket_access
        from ticketing.access import ticket_open_to

        ticket_id = await self._get_ticket_id_from_interaction(interaction)
        if not ticket_id:
            await interaction.response.send_message(
                "Could not identify ticket from this message.",
                ephemeral=True,
            )
            return

        ticket = await Ticket.objects.select_related("team").filter(id=ticket_id).afirst()
        if not ticket:
            await interaction.response.send_message("Ticket not found.", ephemeral=True)
            return

        is_ops, team = await ticket_access(interaction.user.id)
        if not ticket_open_to(ticket, staff=is_ops, team=team):
            await interaction.response.send_message(
                "You must be ticket staff or on this ticket's team to cancel it.", ephemeral=True
            )
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
    def __init__(self, ticket: Ticket) -> None:
        super().__init__()
        self.ticket = ticket
        cat = ticket.category

        self.notes: discord.ui.TextInput[ResolveTicketModal] = discord.ui.TextInput(
            label="Resolution Notes",
            placeholder="Describe how the issue was resolved...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )

        self.points: discord.ui.TextInput[ResolveTicketModal]
        if cat and cat.variable_points:
            min_pts = cat.min_points
            max_pts = cat.max_points
            placeholder = f"Enter points ({min_pts}-{max_pts})" if max_pts else f"Enter points (min {min_pts})"
            self.points = discord.ui.TextInput(
                label="Points",
                placeholder=placeholder,
                required=True,
                max_length=5,
            )
        else:
            # Show fixed points that will be charged if left blank
            fixed_pts = cat.points if cat else 0
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
