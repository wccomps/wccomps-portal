"""Help panels cog - persistent button panels for blue teams."""

import logging
from collections.abc import Awaitable, Callable
from typing import cast

import discord
from asgiref.sync import sync_to_async
from discord.ext import commands
from django.conf import settings

from bot.permissions import linked_team_member
from bot.thread_creator import publish_new_ticket
from bot.ticket_input import read_ticket
from bot.utils import DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT, DISCORD_SELECT_OPTION_LIMIT, fit_lines
from core.tickets_config import TicketCategoryConfig, get_all_categories
from ticketing.utils import TicketRateLimitError

logger = logging.getLogger(__name__)


async def create_ticket(
    interaction: discord.Interaction,
    category_id: str,
    service_name: str = "",
    description: str = "",
    hostname: str = "",
    ip_address: str = "",
) -> None:
    """Create a ticket from a Discord modal submission."""
    await interaction.response.defer(ephemeral=True)

    member = await linked_team_member(interaction.user.id)
    if not member:
        await interaction.followup.send(
            "You must be linked to a competition team to create tickets.\nClick the **Link Account** button first.",
            ephemeral=True,
        )
        return

    try:
        ticket_input = await sync_to_async(read_ticket)(
            category_id,
            staff=False,
            description=description,
            hostname=hostname,
            ip_address=ip_address,
            service_name=service_name,
        )
        if isinstance(ticket_input, str):
            await interaction.followup.send(ticket_input, ephemeral=True)
            return
        cat_info = ticket_input.config

        field_values = {
            "service_name": ticket_input.service_name,
            "description": ticket_input.description,
            "hostname": ticket_input.hostname,
            "ip_address": ticket_input.ip_address,
        }
        required_fields = cat_info.get("required_fields", [])
        missing_fields = [f for f in required_fields if not field_values.get(f)]
        if missing_fields:
            await interaction.followup.send(
                f"Missing required fields: {', '.join(missing_fields)}",
                ephemeral=True,
            )
            return

        from ticketing.utils import acreate_ticket_atomic

        ticket = await acreate_ticket_atomic(
            team=member.team,
            category=ticket_input.category,
            title=cat_info["display_name"],
            description=ticket_input.description,
            hostname=ticket_input.hostname,
            ip_address=ticket_input.ip_address,
            service_name=ticket_input.service_name,
            actor_username=f"discord:{interaction.user.name}",
        )

    except TicketRateLimitError as e:
        logger.warning(f"Ticket rate limit hit by {interaction.user.name} for {member.team.team_name}")
        await interaction.followup.send(str(e), ephemeral=True)
        return
    except Exception as e:
        logger.error(f"Failed to create ticket: {e}", exc_info=True)
        await interaction.followup.send(f"Failed to create ticket: {e!s}", ephemeral=True)
        return

    await interaction.followup.send(
        f"Ticket **{ticket.ticket_number}** created.\n"
        f"Category: **{cat_info['display_name']}**\n"
        f"Points: **{cat_info.get('points', 0)}**\n\n"
        f"A volunteer will respond shortly.",
        ephemeral=True,
    )
    await publish_new_ticket(interaction.client, interaction.guild, ticket)


class ServiceScoringModal(discord.ui.Modal, title="Service Scoring Validation"):
    """Modal for service scoring validation tickets."""

    category_id: str = ""

    service_name: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Service Name",
        placeholder="e.g., HTTP, DNS, SSH",
        required=True,
        max_length=100,
    )

    description: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Description (optional)",
        placeholder="Additional details about the issue",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(
            interaction,
            category_id=self.category_id,
            service_name=self.service_name.value,
            description=self.description.value,
        )


class BoxResetModal(discord.ui.Modal, title="Box Reset / Scrub"):
    category_id: str = ""

    hostname: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Hostname",
        placeholder="e.g., web01, dc01",
        required=True,
        max_length=255,
    )

    ip_address: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="IP Address",
        placeholder="e.g., 10.0.1.50",
        required=True,
        max_length=50,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(
            interaction,
            category_id=self.category_id,
            hostname=self.hostname.value,
            ip_address=self.ip_address.value,
        )


class ScoringServiceCheckModal(discord.ui.Modal, title="Scoring Service Check"):
    """Modal for scoring service check tickets."""

    category_id: str = ""

    service_name: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Service Name",
        placeholder="e.g., HTTP, DNS, SSH",
        required=True,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(
            interaction,
            category_id=self.category_id,
            service_name=self.service_name.value,
        )


class ConsultationModal(discord.ui.Modal, title="Consultation Request"):
    """Modal for consultation tickets."""

    description: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Description",
        placeholder="Describe what you need help with",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    hostname: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Hostname (hands-on only)",
        placeholder="Leave blank for phone consultation",
        required=False,
        max_length=255,
    )

    def __init__(self, category_id: str, cat_info: TicketCategoryConfig):
        super().__init__()
        self.category_id = category_id
        # Remove hostname field if not required (e.g., phone consultation)
        if "hostname" not in cat_info.get("required_fields", []):
            self.title = cat_info["display_name"]
            self.remove_item(self.hostname)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Only include hostname if it's still in the modal
        has_hostname = any(
            isinstance(item, discord.ui.TextInput) and item.label == "Hostname (hands-on only)"
            for item in self.children
        )
        await create_ticket(
            interaction,
            category_id=self.category_id,
            description=self.description.value,
            hostname=self.hostname.value if has_hostname else "",
        )


class OtherModal(discord.ui.Modal, title="Other / General Issue"):
    category_id: str = ""

    description: discord.ui.TextInput[discord.ui.Modal] = discord.ui.TextInput(
        label="Description",
        placeholder="Describe your issue or request",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(
            interaction,
            category_id=self.category_id,
            description=self.description.value,
        )


class CategorySelect(discord.ui.Select["TicketCategoryView"]):
    def __init__(self, categories: dict[int, TicketCategoryConfig]) -> None:
        self.categories = categories
        options = []
        # A select holds 25 options: the first 25 categories in their sort order
        for cat_id, cat_info in list(categories.items())[:DISCORD_SELECT_OPTION_LIMIT]:
            points = cat_info.get("points", 0)
            options.append(
                discord.SelectOption(
                    label=cat_info["display_name"],
                    value=str(cat_id),
                    description=f"{points} points",
                )
            )

        super().__init__(
            placeholder="Select a ticket category...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        category_id = self.values[0]

        cat_info = self.categories[int(category_id)]
        required = cat_info.get("required_fields", [])

        modal: ServiceScoringModal | BoxResetModal | ScoringServiceCheckModal | ConsultationModal | OtherModal
        if "service_name" in required and "description" in required:
            modal = ServiceScoringModal()
            modal.category_id = category_id
        elif "hostname" in required and "ip_address" in required:
            modal = BoxResetModal()
            modal.category_id = category_id
        elif "service_name" in required:
            modal = ScoringServiceCheckModal()
            modal.category_id = category_id
        elif "hostname" in required:
            modal = ConsultationModal(category_id, cat_info)
        elif "description" in required:
            # Check if it's a consultation type (has optional hostname)
            optional = cat_info.get("optional_fields", [])
            if "hostname" in optional:
                modal = ConsultationModal(category_id, cat_info)
            else:
                modal = OtherModal()
                modal.category_id = category_id
        else:
            modal = OtherModal()
            modal.category_id = category_id

        await interaction.response.send_modal(modal)


class TicketCategoryView(discord.ui.View):
    def __init__(self, categories: dict[int, TicketCategoryConfig]) -> None:
        super().__init__(timeout=300)
        self.add_item(CategorySelect(categories))


class LinkButton(discord.ui.Button["TeamHelpView"]):
    CUSTOM_ID = "help_panel:link"

    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.primary, label="Link Account", custom_id=self.CUSTOM_ID)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.view:
            raise RuntimeError("Button callback invoked without view")
        await self.view.link_account(interaction)


class TicketButton(discord.ui.Button["TeamHelpView"]):
    CUSTOM_ID = "help_panel:ticket"

    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.success, label="Create Ticket", custom_id=self.CUSTOM_ID)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.view:
            raise RuntimeError("Button callback invoked without view")
        await self.view.create_ticket(interaction)


class TeamHelpView(discord.ui.View):
    """Persistent view with help buttons for blue teams."""

    def __init__(self, bot: commands.Bot, show_link: bool = True, show_ticket: bool = True):
        super().__init__(timeout=None)
        self.bot = bot

        if show_link:
            self.add_item(LinkButton())

        if show_ticket:
            self.add_item(TicketButton())

    async def link_account(self, interaction: discord.Interaction) -> None:
        # Import here to avoid circular dependency
        from bot.cogs.linking import LinkingCog

        cog = self.bot.get_cog("LinkingCog")
        if not isinstance(cog, LinkingCog):
            await interaction.response.send_message("Linking system is not available.", ephemeral=True)
            return

        callback = cast(
            Callable[[LinkingCog, discord.Interaction], Awaitable[None]],
            cog.link_command.callback,
        )
        await callback(cog, interaction)

    async def create_ticket(self, interaction: discord.Interaction) -> None:
        """Handle create ticket button click - show category selection."""
        categories = await sync_to_async(get_all_categories)(user_creatable_only=True)
        await interaction.response.send_message(
            "Select a ticket category:",
            view=TicketCategoryView(categories),
            ephemeral=True,
        )


def _has_button(message: discord.Message, custom_id: str) -> bool:
    """Whether the message carries the button: how a panel posted earlier is found, whatever its title was then."""
    return any(
        getattr(child, "custom_id", None) == custom_id
        for row in message.components
        for child in getattr(row, "children", ())
    )


class HelpPanelsCog(commands.Cog):
    """Manages persistent help panels for blue teams."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        # Register persistent views
        self.bot.add_view(TeamHelpView(self.bot, show_link=True, show_ticket=False))
        self.bot.add_view(TeamHelpView(self.bot, show_link=False, show_ticket=True))
        self.bot.add_view(TeamHelpView(self.bot, show_link=True, show_ticket=True))

        logger.info("Help panel views registered")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Post (or refresh) the help panels whenever the bot connects; on_ready also fires on reconnect."""
        try:
            # Post link panel to link channel (hidden after linking)
            link_channel_id = getattr(settings, "DISCORD_LINK_CHANNEL_ID", None)
            if link_channel_id:
                await self._post_link_panel(link_channel_id)

            # Post ticket panel to welcome-rules (always visible)
            welcome_channel_id = getattr(settings, "DISCORD_WELCOME_CHANNEL_ID", None)
            if welcome_channel_id:
                await self._post_ticket_panel(welcome_channel_id)

        except Exception as e:
            logger.error(f"Failed to post help panels: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Delete messages posted to #link channel and trigger link flow."""
        if message.author.bot:
            return

        link_channel_id = getattr(settings, "DISCORD_LINK_CHANNEL_ID", None)
        if link_channel_id and message.channel.id == link_channel_id:
            try:
                await message.delete()
                logger.info(f"Deleted message from {message.author} in #link channel")
            except discord.HTTPException as e:
                logger.exception(f"Failed to delete message in #link: {e}")
                return

            from bot.cogs.linking import LinkingCog

            cog = self.bot.get_cog("LinkingCog")
            if isinstance(cog, LinkingCog):
                await cog.send_link_dm(message.author)

    async def _post_link_panel(self, channel_id: int) -> None:
        channel = self.bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            logger.warning(f"Link channel {channel_id} not found or not a text channel")
            return

        embed = discord.Embed(
            title="Link Your Discord Account",
            description=(
                "**Welcome to WCComps!**\n\n"
                "Before you can participate, you need to link your Discord account to your Authentik account.\n\n"
                "**Click the button below to get started:**"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="What happens when I link?",
            value=(
                "• You'll be redirected to Authentik to authenticate\n"
                "• Your Discord account will be linked to your team\n"
                "• While your team is competing, you get your team role and channels within a few minutes"
            ),
            inline=False,
        )

        view = TeamHelpView(self.bot, show_link=True, show_ticket=False)

        async for message in channel.history(limit=10):
            if message.author == self.bot.user and _has_button(message, LinkButton.CUSTOM_ID):
                await message.edit(embed=embed, view=view)
                logger.info(f"Updated link panel in channel {channel_id}")
                return

        await channel.send(embed=embed, view=view)
        logger.info(f"Posted link panel to channel {channel_id}")

    async def _post_ticket_panel(self, channel_id: int) -> None:
        channel = self.bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            logger.warning(f"Ticket channel {channel_id} not found or not a text channel")
            return

        categories_text = []
        categories = await sync_to_async(get_all_categories)(user_creatable_only=True)
        for cat_info in categories.values():
            points = cat_info.get("points", 0)
            categories_text.append(f"• **{cat_info['display_name']}**: {points}pt")

        intro = (
            "**Create a support ticket and our volunteers will assist you!**\n\n"
            "Click the button below to create a ticket. A volunteer will respond shortly.\n\n"
            "**Available Categories:**\n"
        )
        embed = discord.Embed(
            title="Need Help?",
            description=intro + fit_lines(categories_text, DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT - len(intro)),
            color=discord.Color.green(),
        )

        view = TeamHelpView(self.bot, show_link=False, show_ticket=True)

        async for message in channel.history(limit=10):
            if message.author == self.bot.user and _has_button(message, TicketButton.CUSTOM_ID):
                await message.edit(embed=embed, view=view)
                logger.info(f"Updated ticket panel in channel {channel_id}")
                return

        await channel.send(embed=embed, view=view)
        logger.info(f"Posted ticket panel to channel {channel_id}")

    async def post_team_ticket_panel(self, team_channel_id: int) -> None:
        """Post ticket panel to a team's channel (called when team is created)."""
        await self._post_ticket_panel(team_channel_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpPanelsCog(bot))
