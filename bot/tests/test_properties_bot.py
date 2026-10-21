"""Property tests for every bot entry point: each slash command, button, select, modal and message listener, driven
with hostile input as every kind of Discord user (bot_harness says what holds).

A new entry point needs a row in CASES: test_property_coverage fails without one. A row says how to reach it and
which roles may get past its checks; a command's options are read from the command, so a row names only hints, the
valid values that get past its lookups.
"""

from typing import Any

import discord
import pytest
from hypothesis import given
from hypothesis import strategies as st

from bot.cogs.admin import AdminCog
from bot.cogs.admin_competition import AdminCompetitionCog
from bot.cogs.admin_teams import AdminTeamsCog
from bot.cogs.admin_tickets import AdminTicketsCog
from bot.cogs.help_panels import (
    CategorySelect,
    HelpPanelsCog,
    LinkButton,
    TeamHelpView,
    TicketButton,
    TicketCategoryView,
    TicketModal,
)
from bot.cogs.linking import LinkingCog
from bot.cogs.scoring import ScoringCog
from bot.cogs.ticketing import TicketingCog
from bot.tests.bot_harness import (
    EVERYONE,
    TEAM_MEMBERS,
    BotCase,
    Command,
    Draws,
    Item,
    Listener,
    Modal,
    Run,
    World,
    category_ids,
    dashboard_view,
    drive,
    let_in,
    may,
    mentions_of,
    on_message,
    on_message_delete,
    on_message_edit,
    stored_text,
    team_ranges,
    ticket_numbers,
)
from bot.ticket_dashboard import ResolveTicketModal, TicketActionView
from bot.unified_dashboard import DashboardControlView
from bot.utils import CancelButton, ConfirmButton, ConfirmView
from core.tests.strategies import datetime_text
from core.tests.views_harness import views_profile
from ticketing.access import TICKET_STAFF
from ticketing.models import TicketCategory

ADMIN = may("admin")
TICKETING_ADMIN = may("ticketing_admin")
STAFF = may(TICKET_STAFF)


def teams(world: World) -> st.SearchStrategy[int]:
    return st.sampled_from([t.team_number for t in world.teams] + [4, 0, 51])


def user_ids(world: World) -> st.SearchStrategy[str]:
    return st.sampled_from([str(i) for i in world.discord_ids.values()] + ["123", "not a user"])


def slugs(_: World) -> st.SearchStrategy[str]:
    return st.sampled_from(("netbird", "netbird,scoring", " , ", ",,", "a" * 300))


def broadcast_targets(_: World) -> st.SearchStrategy[str]:
    return st.one_of(st.sampled_from(("announcements", " ANNOUNCEMENTS ", "all-teams")), team_ranges())


# ---------------------------------------------------------------------------------------------------------------
# Views and messages the items are on
# ---------------------------------------------------------------------------------------------------------------


def ticket_message(run: Run, data: Draws) -> Any:
    """The message a persistent ticket view is on after a restart: the ticket number read back from its embed."""
    # The bot wrote the message: its title holds no NUL (a ticket number or a title from the database)
    number = data.draw(st.one_of(ticket_numbers(run.world), stored_text(30)), label="embed ticket")
    message = run.discord.message_object()
    message.embeds = [discord.Embed(title=f"Ticket {number}: Title")]
    return message


def ticket_view(run: Run, data: Draws) -> TicketActionView:
    """A view made with its ticket, or the persistent one registered at startup (ticket 0), which reads the ticket
    from its message."""
    name = data.draw(st.sampled_from([None, *sorted(run.world.tickets)]), label="view ticket")
    return TicketActionView(run.world.tickets[name].pk if name else 0)


def help_view(run: Run, data: Draws) -> TeamHelpView:
    return TeamHelpView(run.discord.bot)


def category_view(run: Run, data: Draws) -> TicketCategoryView:
    return TicketCategoryView(list(TicketCategory.objects.filter(user_creatable=True).order_by("sort_order", "id")))


def confirm_view(run: Run, data: Draws) -> ConfirmView:
    return ConfirmView()


def resolve_modal(run: Run, data: Draws) -> ResolveTicketModal:
    ticket = run.world.tickets[data.draw(st.sampled_from(sorted(run.world.tickets)), label="ticket")]
    return ResolveTicketModal(ticket)


def help_modal(run: Run, data: Draws) -> TicketModal:
    category_id = data.draw(st.sampled_from(run.world.user_creatable), label="category")
    return TicketModal(TicketCategory.objects.get(pk=category_id))


# ---------------------------------------------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------------------------------------------

CASES: list[BotCase] = [
    # Slash commands
    Command(AdminCog, AdminCog.admin_sync_roles.callback, ADMIN),
    Command(AdminCompetitionCog, AdminCompetitionCog.admin_broadcast.callback, ADMIN, {"target": broadcast_targets}),
    Command(AdminCompetitionCog, AdminCompetitionCog.admin_cleanup_competition.callback, ADMIN),
    Command(AdminCompetitionCog, AdminCompetitionCog.admin_competition_set_apps.callback, ADMIN, {"app_slugs": slugs}),
    Command(
        AdminCompetitionCog,
        AdminCompetitionCog.admin_reset_blueteam_passwords.callback,
        may("gold_team"),
        {"team_numbers": lambda w: team_ranges()},
    ),
    Command(
        AdminCompetitionCog,
        AdminCompetitionCog.admin_set_end_time.callback,
        ADMIN,
        {"datetime_str": lambda w: datetime_text()},
    ),
    Command(AdminCompetitionCog, AdminCompetitionCog.admin_set_max_members.callback, ADMIN),
    Command(
        AdminCompetitionCog,
        AdminCompetitionCog.admin_set_start_time.callback,
        ADMIN,
        {"datetime_str": lambda w: datetime_text()},
    ),
    Command(AdminCompetitionCog, AdminCompetitionCog.admin_start_competition.callback, ADMIN),
    Command(AdminCompetitionCog, AdminCompetitionCog.admin_stop_competition.callback, ADMIN),
    Command(AdminTeamsCog, AdminTeamsCog.admin_activate_teams.callback, ADMIN, {"teams": lambda w: team_ranges()}),
    Command(AdminTeamsCog, AdminTeamsCog.admin_deactivate_teams.callback, ADMIN, {"teams": lambda w: team_ranges()}),
    Command(AdminTeamsCog, AdminTeamsCog.admin_recreate_teams.callback, ADMIN, {"teams": lambda w: team_ranges()}),
    Command(AdminTeamsCog, AdminTeamsCog.admin_remove_team.callback, ADMIN, {"team_number": teams}),
    Command(AdminTeamsCog, AdminTeamsCog.admin_reset_team.callback, ADMIN, {"team_number": teams}),
    Command(AdminTeamsCog, AdminTeamsCog.admin_team_info.callback, ADMIN, {"team_number": teams}),
    Command(AdminTeamsCog, AdminTeamsCog.admin_teams.callback, ADMIN),
    Command(AdminTeamsCog, AdminTeamsCog.admin_unlink.callback, ADMIN, {"users": mentions_of}),
    Command(
        AdminTicketsCog,
        AdminTicketsCog.admin_change_category.callback,
        TICKETING_ADMIN,
        {"ticket_number": ticket_numbers, "new_category": category_ids},
    ),
    Command(
        AdminTicketsCog,
        AdminTicketsCog.admin_ticket_cancel.callback,
        TICKETING_ADMIN,
        {"ticket_number": ticket_numbers},
    ),
    Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_clear.callback, TICKETING_ADMIN),
    Command(
        AdminTicketsCog,
        AdminTicketsCog.admin_ticket_create.callback,
        TICKETING_ADMIN,
        {"team_number": teams, "category": category_ids},
    ),
    Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_list.callback, STAFF, {"team_number": teams}),
    Command(
        AdminTicketsCog,
        AdminTicketsCog.admin_ticket_reassign.callback,
        TICKETING_ADMIN,
        {"ticket_number": ticket_numbers, "volunteer": user_ids},
    ),
    Command(
        AdminTicketsCog,
        AdminTicketsCog.admin_ticket_reopen.callback,
        TICKETING_ADMIN,
        {"ticket_number": ticket_numbers},
    ),
    Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_resolve.callback, STAFF, {"ticket_number": ticket_numbers}),
    Command(LinkingCog, LinkingCog.link_command.callback, EVERYONE),
    Command(ScoringCog, ScoringCog.incident_report.callback, TEAM_MEMBERS),
    Command(TicketingCog, TicketingCog.create_ticket.callback, TEAM_MEMBERS, {"category": category_ids}),
    # Buttons and the select
    Item("button", LinkButton, help_view, EVERYONE),
    Item("button", TicketButton, help_view, EVERYONE),
    Item("button", TicketActionView.claim_button, ticket_view, STAFF, message=ticket_message),
    Item("button", TicketActionView.resolve_button, ticket_view, STAFF, message=ticket_message),
    Item("button", TicketActionView.cancel_button, ticket_view, STAFF | TEAM_MEMBERS, message=ticket_message),
    *(
        Item("button", getattr(DashboardControlView, name), dashboard_view, EVERYONE)
        for name in ("sort_created", "sort_stale", "sort_team", "filter_all", "filter_open", "filter_claimed")
    ),
    Item("button", ConfirmButton, confirm_view, ADMIN | TICKETING_ADMIN, reached_by=ADMIN | TICKETING_ADMIN),
    Item("button", CancelButton, confirm_view, ADMIN | TICKETING_ADMIN, reached_by=ADMIN | TICKETING_ADMIN),
    Item("select", CategorySelect, category_view, EVERYONE),
    # Modals
    Modal(TicketModal, help_modal, TEAM_MEMBERS),
    Modal(ResolveTicketModal, resolve_modal, STAFF, reached_by=STAFF),
    # Message listeners
    Listener(HelpPanelsCog, HelpPanelsCog.on_message, on_message),
    Listener(TicketingCog, TicketingCog.on_message, on_message),
    Listener(TicketingCog, TicketingCog.on_message_edit, on_message_edit),
    Listener(TicketingCog, TicketingCog.on_message_delete, on_message_delete),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
@views_profile()
@given(data=st.data())
def test_any_input_is_answered_within_discords_limits(case: BotCase, bot_world: World, data: st.DataObject) -> None:
    """Each example runs a whole handler against the fakes, so the rows run the view tests' fewer examples (the
    -views profile)."""
    drive(case, bot_world, data)


COMMANDS = [case for case in CASES if isinstance(case, Command)]


@pytest.mark.parametrize("case", COMMANDS, ids=lambda c: c.name)
def test_each_command_lets_in_exactly_its_roles(case: Command, bot_world: World) -> None:
    from bot.tests.bot_harness import BOT_ROLES

    let = {role for role in BOT_ROLES if let_in(case, bot_world, role)}
    assert let == case.allowed, f"{case.name}: the checks let in {sorted(let)}, the row says {sorted(case.allowed)}"
