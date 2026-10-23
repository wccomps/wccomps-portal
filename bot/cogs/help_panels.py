"""Help panels cog - persistent button panels for blue teams."""

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import cast

import discord
from asgiref.sync import sync_to_async
from discord.ext import commands
from django.conf import settings

from bot.permissions import linked_team_member
from bot.thread_creator import publish_new_ticket
from bot.ticket_input import read_ticket
from bot.utils import DISCORD_EMBED_DESCRIPTION_CHAR_LIMIT, DISCORD_SELECT_OPTION_LIMIT, fit, fit_lines
from ticketing.models import TicketCategory
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
        category = ticket_input.category

        from ticketing.utils import acreate_ticket_atomic

        ticket = await acreate_ticket_atomic(
            team=member.team,
            category=category,
            title=category.display_name,
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
        f"Category: **{category.display_name}**\n"
        f"Points: **{category.points_label}**\n\n"
        f"A volunteer will respond shortly.",
        ephemeral=True,
    )
    await publish_new_ticket(interaction.client, interaction.guild, ticket)


_MODAL_FIELD_SPECS: dict[str, tuple[str, str, discord.TextStyle, int]] = {
    "service_name": ("Service Name", "e.g., HTTP, DNS, SSH", discord.TextStyle.short, 100),
    "hostname": ("Hostname", "e.g., web01, dc01", discord.TextStyle.short, 255),
    "ip_address": ("IP Address", "e.g., 10.0.1.50", discord.TextStyle.short, 45),
    "description": ("Description", "Describe your issue or request", discord.TextStyle.paragraph, 1000),
}


class TicketModal(discord.ui.Modal):
    """Modal for creating a ticket in a category, showing its required and optional fields."""

    def __init__(self, category: TicketCategory) -> None:
        super().__init__(title=fit(category.display_name, 45))
        self.category_id = str(category.pk)
        self.inputs: dict[str, discord.ui.TextInput[TicketModal]] = {}

        required = set(category.required_fields)
        fields = [
            f for f in dict.fromkeys([*category.required_fields, *category.optional_fields]) if f in _MODAL_FIELD_SPECS
        ]
        if not fields:
            fields = ["description"]
            required.add("description")

        for name in fields:
            label, placeholder, style, max_length = _MODAL_FIELD_SPECS[name]
            # IP Address is left optional in the modal when Hostname is present so Quotient can auto-fill it
            is_required = name in required and not (name == "ip_address" and "hostname" in fields)
            text_input: discord.ui.TextInput[TicketModal] = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                style=style,
                required=is_required,
                max_length=max_length,
            )
            self.inputs[name] = text_input
            self.add_item(text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await create_ticket(
            interaction,
            category_id=self.category_id,
            service_name=self.inputs["service_name"].value if "service_name" in self.inputs else "",
            description=self.inputs["description"].value if "description" in self.inputs else "",
            hostname=self.inputs["hostname"].value if "hostname" in self.inputs else "",
            ip_address=self.inputs["ip_address"].value if "ip_address" in self.inputs else "",
        )


class CategorySelect(discord.ui.Select["TicketCategoryView"]):
    def __init__(self, categories: Iterable[TicketCategory]) -> None:
        # A select holds 25 options: the first 25 categories in their sort order
        self.categories = {cat.pk: cat for cat in list(categories)[:DISCORD_SELECT_OPTION_LIMIT]}
        options = [
            discord.SelectOption(
                label=cat.display_name,
                value=str(cat.pk),
                description=cat.points_display,
            )
            for cat in self.categories.values()
        ]

        super().__init__(
            placeholder="Select a ticket category...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        category = self.categories[int(self.values[0])]
        await interaction.response.send_modal(TicketModal(category))


class TicketCategoryView(discord.ui.View):
    def __init__(self, categories: Iterable[TicketCategory]) -> None:
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
        categories = [cat async for cat in TicketCategory.objects.filter(user_creatable=True)]
        if not categories:
            await interaction.response.send_message(
                "No ticket categories are currently available.",
                ephemeral=True,
            )
            return
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
        self.bot.add_view(TeamHelpView(self.bot, show_link=True, show_ticket=False))
        self.bot.add_view(TeamHelpView(self.bot, show_link=False, show_ticket=True))
        self.bot.add_view(TeamHelpView(self.bot, show_link=True, show_ticket=True))

        logger.info("Help panel views registered")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Post (or refresh) the help panels whenever the bot connects; on_ready also fires on reconnect."""
        try:
            link_channel_id = getattr(settings, "DISCORD_LINK_CHANNEL_ID", None)
            if link_channel_id:
                await self._post_link_panel(link_channel_id)

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

        categories_text = [
            f"• **{cat.display_name}**: {cat.points_label}"
            async for cat in TicketCategory.objects.filter(user_creatable=True)
        ]

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
