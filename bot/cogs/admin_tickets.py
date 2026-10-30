import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.permissions import permission_check
from bot.ticket_dashboard import trigger_dashboard
from bot.ticket_input import file_ticket, read_resolution
from bot.utils import (
    DISCORD_EMBED_FIELD_COUNT_LIMIT,
    ConfirmView,
    add_field,
    choices,
    find_team,
    fit,
    log_to_ops_channel,
    run_blocking,
)
from ticketing.access import TICKET_STAFF
from ticketing.models import Ticket, TicketCategory, TicketHistory

logger = logging.getLogger(__name__)

# A ticket number option, as long as the column (Discord refuses a longer one)
TicketNumber = app_commands.Range[str, 1, Ticket._meta.get_field("ticket_number").max_length]
LIST_FOOTER_ROOM = 100  # room in the list embed's 6000-char budget for the "Showing N of M" footer


class UserOrIdTransformer(app_commands.Transformer):
    """Transform either a User mention or a Discord ID string into a User object."""

    async def transform(self, interaction: discord.Interaction, value: discord.User | str) -> discord.User | None:
        if isinstance(value, discord.User):
            return value

        try:
            user_id = int(value)
            return await interaction.client.fetch_user(user_id)
        except (ValueError, discord.NotFound, discord.HTTPException) as e:
            raise app_commands.AppCommandError("Invalid user. Please provide a @mention or valid Discord ID.") from e


class AdminTicketsCog(commands.Cog):
    tickets_group = app_commands.Group(name="tickets", description="Ticket management commands")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def admin_category_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for ticket category (admin - all categories)."""
        categories = [cat async for cat in TicketCategory.objects.order_by("sort_order", "id")]
        return choices(
            [
                (cat.display_name, str(cat.id))
                for cat in categories
                if not current or current.lower() in cat.display_name.lower()
            ]
        )

    @tickets_group.command(name="create", description="[ADMIN] Create a ticket for a team")
    @app_commands.describe(
        team_number="Team number (1-50)",
        category="Ticket category",
        description="Description of the issue",
        hostname="Hostname/box name (required for box-reset, hands-on consultation)",
        service="Service name like 'web:http' (required for scoring validation, service check)",
        ip_address="IP address (auto-filled from hostname, or enter manually)",
    )
    @app_commands.autocomplete(category=admin_category_autocomplete)
    @app_commands.check(permission_check("ticketing_admin"))
    async def admin_ticket_create(
        self,
        interaction: discord.Interaction,
        team_number: int,
        category: str,
        description: str,
        hostname: str | None = None,
        service: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Create a ticket as admin."""
        if not interaction.guild:
            await interaction.response.send_message("This command must be used in a guild", ephemeral=True)
            return
        # Discord rejects a first answer after 3 seconds
        await interaction.response.defer(ephemeral=True)

        team = await find_team(team_number)
        if isinstance(team, str):
            await interaction.followup.send(team, ephemeral=True)
            return

        filed = await run_blocking(
            file_ticket,
            team,
            category,
            staff=True,
            actor=f"admin:{interaction.user}",
            description=description,
            hostname=hostname or "",
            ip_address=ip_address or "",
            service_name=service or "",
        )
        if isinstance(filed, str):
            await interaction.followup.send(filed, ephemeral=True)
            return
        ticket, cat = filed

        await log_to_ops_channel(
            self.bot,
            f"Admin Ticket Created: {ticket.ticket_number} ({cat.display_name}) "
            f"for **{team.team_name}** by {interaction.user.mention}",
        )
        await interaction.followup.send(
            f"Created ticket **{ticket.ticket_number}** for **{team.team_name}**\n"
            f"Category: {cat.display_name}\n"
            f"Point cost: {cat.points_display}",
            ephemeral=True,
        )

    @tickets_group.command(name="list", description="[ADMIN] List open tickets")
    @app_commands.describe(status="Filter by status", team_number="Filter by team number")
    @app_commands.choices(
        status=[
            app_commands.Choice(name="Open", value="open"),
            app_commands.Choice(name="Claimed", value="claimed"),
            app_commands.Choice(name="Resolved", value="resolved"),
            app_commands.Choice(name="All", value="all"),
        ]
    )
    @app_commands.check(permission_check(TICKET_STAFF))
    async def admin_ticket_list(
        self,
        interaction: discord.Interaction,
        status: str = "open",
        team_number: int | None = None,
    ) -> None:
        """List tickets with optional filters."""

        query = Ticket.objects.select_related("team", "assigned_to", "category")
        if status != "all":
            query = query.filter(status=status)
        if team_number:
            query = query.filter(team__team_number=team_number)

        total_count = await query.acount()

        if total_count == 0:
            await interaction.response.send_message("No tickets found matching criteria", ephemeral=True)
            return

        # An embed holds 25 fields and 6000 characters: as many of the newest as fit
        tickets = [t async for t in query.order_by("-created_at")[:DISCORD_EMBED_FIELD_COUNT_LIMIT]]
        embed = discord.Embed(title=f"Tickets ({status}): {total_count} in all", color=discord.Color.blue())

        shown = 0
        for ticket in tickets:
            cat_name = ticket.category.display_name if ticket.category else f"Category {ticket.category_id}"
            value = (
                f"Team: {ticket.team.team_name}\n"
                f"Category: {cat_name}\n"
                f"Status: {ticket.status}\n"
                f"Created: {discord.utils.format_dt(ticket.created_at, style='R')}"
            )
            if ticket.assigned_to:
                value += f"\nAssigned: {ticket.assigned_to.username}"
            if not add_field(embed, f"{ticket.ticket_number}: {ticket.title}", value, room=LIST_FOOTER_ROOM):
                break
            shown += 1

        if shown < total_count:
            embed.set_footer(text=f"Showing {shown} of {total_count}: the web interface lists them all")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tickets_group.command(name="resolve", description="[ADMIN] Resolve a ticket and apply points")
    @app_commands.describe(
        ticket_number="Ticket number (e.g., T050-003)",
        notes="Resolution notes",
        points="Point value (required for variable categories; overrides the default otherwise)",
    )
    @app_commands.check(permission_check(TICKET_STAFF))
    async def admin_ticket_resolve(
        self,
        interaction: discord.Interaction,
        ticket_number: TicketNumber,
        notes: str = "",
        points: int | None = None,
    ) -> None:
        """Resolve a ticket and apply point adjustments."""
        from ticketing.lifecycle import aresolve_ticket

        ticket = await Ticket.objects.filter(ticket_number=ticket_number).afirst()
        if not ticket:
            await interaction.response.send_message(f"Ticket {ticket_number} not found", ephemeral=True)
            return
        resolution = read_resolution(notes, "" if points is None else str(points))
        if isinstance(resolution, str):
            await interaction.response.send_message(resolution, ephemeral=True)
            return

        resolved, error = await aresolve_ticket(
            ticket_id=ticket.id,
            actor_username=str(interaction.user),
            resolution_notes=resolution[0],
            points_override=resolution[1],
            discord_id=interaction.user.id,
            discord_username=str(interaction.user),
        )
        if error or resolved is None:
            await interaction.response.send_message(error or "Failed to resolve ticket.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Resolved ticket {resolved.ticket_number}\n"
            f"Point Penalty: {resolved.points_charged} points applied to {resolved.team.team_name}",
            ephemeral=True,
        )
        await log_to_ops_channel(
            self.bot,
            f"Ticket Resolved: {resolved.ticket_number} for **{resolved.team.team_name}** by "
            f"{interaction.user.mention}\nPoint Penalty: {resolved.points_charged} points",
        )

    @tickets_group.command(name="cancel", description="[ADMIN] Cancel a ticket without applying points")
    @app_commands.describe(
        ticket_number="Ticket number (e.g., T050-003)",
        reason="Reason for cancellation",
    )
    @app_commands.check(permission_check("ticketing_admin"))
    async def admin_ticket_cancel(
        self, interaction: discord.Interaction, ticket_number: TicketNumber, reason: str = ""
    ) -> None:
        """Cancel a ticket without point penalty."""
        from ticketing.lifecycle import acancel_ticket

        ticket = await Ticket.objects.filter(ticket_number=ticket_number).afirst()
        if not ticket:
            await interaction.response.send_message(f"Ticket {ticket_number} not found", ephemeral=True)
            return

        cancelled, error = await acancel_ticket(
            ticket_id=ticket.id,
            actor_username=str(interaction.user),
            reason=reason,
            staff=True,
        )
        if error or cancelled is None:
            await interaction.response.send_message(error or "Failed to cancel ticket.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Cancelled ticket {cancelled.ticket_number} (no point penalty applied)",
            ephemeral=True,
        )
        await log_to_ops_channel(
            self.bot,
            f"Ticket Cancelled: {cancelled.ticket_number} for **{cancelled.team.team_name}** by "
            f"{interaction.user.mention}\nReason: {reason or 'No reason provided'}",
        )

    @tickets_group.command(
        name="change-category",
        description="[ADMIN] Change the category of a ticket",
    )
    @app_commands.describe(
        ticket_number="Ticket number (e.g., T050-003)",
        new_category="New category for the ticket",
    )
    @app_commands.autocomplete(new_category=admin_category_autocomplete)
    @app_commands.check(permission_check("ticketing_admin"))
    async def admin_change_category(
        self, interaction: discord.Interaction, ticket_number: TicketNumber, new_category: str
    ) -> None:
        """Change the category of a ticket."""
        from ticketing.forms import TicketChangeCategoryForm
        from ticketing.lifecycle import achange_ticket_category_atomic

        ticket = await Ticket.objects.select_related("category").filter(ticket_number=ticket_number).afirst()
        if not ticket:
            await interaction.response.send_message(f"Ticket {ticket_number} not found", ephemeral=True)
            return

        form = TicketChangeCategoryForm({"new_category": new_category})
        if not form.is_valid():
            await interaction.response.send_message("Invalid ticket category.", ephemeral=True)
            return
        old_cat = ticket.category
        old_category_id = ticket.category_id
        new_category_id = form.cleaned_data["new_category"]
        changed, error = await achange_ticket_category_atomic(
            ticket_id=ticket.id,
            new_category_id=new_category_id,
            actor_username=str(interaction.user),
            discord_id=interaction.user.id,
        )
        if error or changed is None:
            await interaction.response.send_message(error or "Failed to change category.", ephemeral=True)
            return
        ticket = changed

        new_cat = ticket.category
        trigger_dashboard(self.bot)

        old_cat_name = old_cat.display_name if old_cat else str(old_category_id)
        new_cat_name = new_cat.display_name if new_cat else str(new_category_id)
        old_pts = old_cat.points_label if old_cat else "0pt"
        new_pts = new_cat.points_label if new_cat else "0pt"

        await log_to_ops_channel(
            self.bot,
            f"Ticket Category Changed: {ticket.ticket_number} for **{ticket.team.team_name}**\n"
            f"Changed by {interaction.user.mention}: from {old_cat_name} to {new_cat_name}\n"
            f"Point impact: from {old_pts} to {new_pts}",
        )

        await interaction.response.send_message(
            f"Changed {ticket.ticket_number} from {old_cat_name} to {new_cat_name}",
            ephemeral=True,
        )

    @tickets_group.command(
        name="reassign",
        description="[ADMIN] Reassign a ticket to a different volunteer",
    )
    @app_commands.describe(
        ticket_number="Ticket number (e.g., T050-003)",
        volunteer="Discord user (@mention or ID) to assign (leave empty to unclaim a claimed ticket)",
    )
    @app_commands.check(permission_check("ticketing_admin"))
    async def admin_ticket_reassign(
        self,
        interaction: discord.Interaction,
        ticket_number: TicketNumber,
        volunteer: app_commands.Transform[discord.User, UserOrIdTransformer] | None = None,
    ) -> None:
        """Reassign a ticket to a different volunteer."""
        await interaction.response.defer(ephemeral=True)

        ticket = await Ticket.objects.select_related("team", "assigned_to").filter(ticket_number=ticket_number).afirst()
        if not ticket:
            await interaction.followup.send(f"Ticket {ticket_number} not found", ephemeral=True)
            return

        # Any ticket can get a new volunteer, resolved and cancelled ones included; with no volunteer only a
        # claimed ticket can be unclaimed (the lifecycle refuses the rest).
        old_assignee = ticket.assigned_to.username if ticket.assigned_to else "Unassigned"

        from ticketing.lifecycle import aassign_ticket, aunclaim_ticket

        if volunteer:
            changed, error = await aassign_ticket(
                ticket_id=ticket.id,
                actor_username=str(interaction.user),
                discord_id=volunteer.id,
                discord_username=str(volunteer),
                actor_discord_id=interaction.user.id,
            )
            failure = "Failed to assign ticket"
            new_assignee = str(volunteer)
        else:
            changed, error = await aunclaim_ticket(
                ticket_id=ticket.id, actor_username=str(interaction.user), discord_id=interaction.user.id
            )
            failure = "Failed to unassign ticket"
            new_assignee = "Unassigned"

        if error or changed is None:
            await interaction.followup.send(f"{failure}: {error or 'Unknown error'}", ephemeral=True)
            return
        ticket = changed

        await interaction.followup.send(
            f"Ticket {ticket.ticket_number} reassigned\n• From: {old_assignee}\n• To: {new_assignee}",
            ephemeral=True,
        )

        await log_to_ops_channel(
            self.bot,
            f"Ticket reassigned by {interaction.user.mention}\n"
            f"• Ticket: {ticket.ticket_number} ({ticket.team.team_name})\n"
            f"• From: {old_assignee}\n"
            f"• To: {new_assignee}",
        )

    @tickets_group.command(name="reopen", description="[ADMIN] Reopen a resolved ticket")
    @app_commands.describe(ticket_number="Ticket number (e.g., T050-003)", reason="Reason for reopening")
    @app_commands.check(permission_check("ticketing_admin"))
    async def admin_ticket_reopen(
        self, interaction: discord.Interaction, ticket_number: TicketNumber, reason: str
    ) -> None:
        """Reopen a resolved ticket."""
        from ticketing.lifecycle import areopen_ticket

        await interaction.response.defer(ephemeral=True)

        ticket = await Ticket.objects.filter(ticket_number=ticket_number).afirst()
        if not ticket:
            await interaction.followup.send(f"Ticket {ticket_number} not found", ephemeral=True)
            return

        refunded = ticket.points_charged
        reopened, error = await areopen_ticket(
            ticket_id=ticket.id, actor_username=str(interaction.user), reopen_reason=reason
        )
        if error or reopened is None:
            await interaction.followup.send(error or "Failed to reopen ticket.", ephemeral=True)
            return

        refund_msg = f"\n• Refunded: {refunded} points" if refunded > 0 else ""
        await interaction.followup.send(
            f"Ticket {reopened.ticket_number} reopened\n• Reason: {fit(reason, 1500)}{refund_msg}",
            ephemeral=True,
        )
        await log_to_ops_channel(
            self.bot,
            f"Ticket reopened by {interaction.user.mention}\n"
            f"• Ticket: {reopened.ticket_number} ({reopened.team.team_name})\n"
            f"• Reason: {reason}{refund_msg}",
        )

    @tickets_group.command(name="clear", description="[ADMIN] Delete all tickets and reset counters")
    @app_commands.check(permission_check("ticketing_admin"))
    async def admin_ticket_clear(self, interaction: discord.Interaction) -> None:
        """Delete all tickets and reset team counters."""
        from team.models import Team
        from ticketing.models import TicketAttachment, TicketComment

        ticket_count = await Ticket.objects.acount()
        attachment_count = await TicketAttachment.objects.acount()
        comment_count = await TicketComment.objects.acount()
        history_count = await TicketHistory.objects.acount()
        teams_to_reset = await Team.objects.filter(ticket_counter__gt=0).acount()

        # Counters can be left with no tickets (data wiped before the wipe also reset them); reset those too
        if ticket_count == 0 and teams_to_reset == 0:
            await interaction.response.send_message("No tickets or ticket counters to clear", ephemeral=True)
            return

        view = ConfirmView(confirm_label="Confirm Delete")
        await interaction.response.send_message(
            f"**WARNING: This will DELETE ALL TICKETS**\n\n"
            f"• Tickets: {ticket_count}\n"
            f"• Attachments: {attachment_count}\n"
            f"• Comments: {comment_count}\n"
            f"• History: {history_count}\n"
            f"• Teams to reset: {teams_to_reset}\n\n"
            f"This action cannot be undone. Are you sure?",
            view=view,
            ephemeral=True,
        )

        await view.wait()

        if view.confirmed is None:
            await interaction.edit_original_response(content="Timed out", view=None)
            return

        if not view.confirmed:
            await interaction.edit_original_response(content="Cancelled", view=None)
            return

        from ticketing.utils import aclear_all_tickets

        counts = await aclear_all_tickets(actor=str(interaction.user))
        ticket_count, teams_to_reset = counts["tickets_deleted"], counts["teams_reset"]

        await interaction.edit_original_response(
            content=f"Cleared all tickets\n• Deleted {ticket_count} tickets\n• Reset {teams_to_reset} team counters",
            view=None,
        )

        await log_to_ops_channel(
            self.bot,
            f"All tickets cleared by {interaction.user.mention}\n"
            f"• Tickets deleted: {ticket_count}\n"
            f"• Teams reset: {teams_to_reset}",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminTicketsCog(bot))
