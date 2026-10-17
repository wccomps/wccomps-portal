"""Regression tests for bugs the bot's property tests found, each pinned with the input that showed it."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from bot.cogs.admin_competition import AdminCompetitionCog
from bot.cogs.admin_teams import AdminTeamsCog
from bot.cogs.admin_tickets import AdminTicketsCog
from bot.cogs.ticketing import TicketingCog
from bot.tests.bot_harness import (
    Chosen,
    Command,
    Listener,
    Modal,
    Run,
    World,
    discord_world,
    drive,
    may,
    mention_problems,
    on_message,
    on_message_edit,
    option_text,
    sent_problems,
)
from bot.ticket_dashboard import ResolveTicketModal
from core.tests.strategies import rolled_back

BROADCAST = Command(AdminCompetitionCog, AdminCompetitionCog.admin_broadcast.callback, may("admin"))
RESOLVE = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_resolve.callback, may("ticketing_support"))
REASSIGN = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_reassign.callback, may("ticketing_admin"))
TICKET = Command(TicketingCog, TicketingCog.create_ticket.callback, frozenset({"blue_team", "other_team"}))
ADMIN_TICKET = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_create.callback, may("ticketing_admin"))
CHANGE_CATEGORY = Command(AdminTicketsCog, AdminTicketsCog.admin_change_category.callback, may("ticketing_admin"))
RESOLVE_MODAL = Modal(ResolveTicketModal, lambda run, data: resolve_modal(run, "claimed"), may("ticketing_support"))
THREAD_MESSAGE = Listener(TicketingCog, TicketingCog.on_message, on_message)
THREAD_EDIT = Listener(TicketingCog, TicketingCog.on_message_edit, on_message_edit)


def resolve_modal(run: Run, name: str) -> ResolveTicketModal:
    from core.tickets_config import get_category_config

    ticket = run.world.tickets[name]
    return ResolveTicketModal(ticket, get_category_config(ticket.category_id) or {})


def in_a_ticket_thread(world: World, content: str, **more: object) -> Chosen:
    thread = world.tickets["open"].discord_thread_id
    return Chosen({"role": "blue_team", "channel": "ticket thread", "thread": thread, "content": content, **more})


@example(message="@everyone the scoring engine is back")
@given(message=option_text(1, 1000))
def test_an_announcement_pings_only_the_blueteam_role(bot_world: World, message: str) -> None:
    """The bot set no default allowed mentions, so any message it sent pinged everything it named: @everyone in a
    broadcast, and every user an ops log line mentioned."""
    drive(BROADCAST, bot_world, Chosen({"role": "admin", "target": "announcements", "message": message}))


@example(ticket_number="T001-001\x00")
@given(ticket_number=option_text(1, 20).map(lambda s: s + "\x00"))
def test_an_option_with_a_nul_is_refused(bot_world: World, ticket_number: str) -> None:
    """PostgreSQL text can't hold a NUL, so a ticket number with one failed the lookup (ValueError) and the command
    never answered."""
    drive(RESOLVE, bot_world, Chosen({"role": "admin", "ticket_number": ticket_number}))


@example(volunteer="not a user")
@given(volunteer=option_text(1, 100).filter(lambda s: not s.strip().isdigit()))
def test_an_option_the_bot_cannot_read_is_answered(bot_world: World, volunteer: str) -> None:
    """A volunteer that isn't a user or an id fails the option's transformer, and the command tree only logged it:
    Discord showed "The application did not respond"."""
    drive(REASSIGN, bot_world, Chosen({"role": "admin", "ticket_number": "T001-002", "volunteer": volunteer}))


@example(content="logs\x00")
@given(content=option_text(1, 2000).map(lambda s: s + "\x00"))
def test_a_thread_message_with_a_nul_is_saved_without_it(bot_world: World, content: str) -> None:
    """A ticket thread message holding a NUL failed to save as a comment (ValueError), so the listener raised."""
    drive(THREAD_MESSAGE, bot_world, in_a_ticket_thread(bot_world, content))
    drive(THREAD_EDIT, bot_world, in_a_ticket_thread(bot_world, content, **{"message id": 1, "was saved": True}))


@example(category="Box Reset")
@given(category=option_text(1, 100))
def test_a_category_that_is_not_a_number_is_refused(bot_world: World, category: str) -> None:
    """Every command that files a ticket or changes its category read the option with int() and raised on
    anything else (autocomplete offers ids, but Discord sends whatever was typed)."""
    drive(TICKET, bot_world, Chosen({"role": "blue_team", "category": category, "description": "Down"}))
    drive(
        ADMIN_TICKET, bot_world, Chosen({"role": "admin", "team_number": 1, "category": category, "description": "x"})
    )
    drive(CHANGE_CATEGORY, bot_world, Chosen({"role": "admin", "ticket_number": "T001-002", "new_category": category}))


@example(hostname="web" * 100)
@given(hostname=option_text(256, 6000))
def test_a_ticket_field_longer_than_its_column_is_refused(bot_world: World, hostname: str) -> None:
    """A slash command option has no length unless it declares one, so a long hostname, service or IP reached the
    database's varchar column (DataError); the web form refuses it, and now so does the bot."""
    chosen = {"role": "blue_team", "category": "2", "description": "Down", "hostname": hostname}
    drive(TICKET, bot_world, Chosen(chosen))


@example(description="Help")
@given(description=option_text(1, 1000))
def test_a_team_files_only_in_a_category_teams_may_create(bot_world: World, description: str) -> None:
    """/ticket took any category id, so a team could file a ticket in a staff-only category the web form hides."""
    from ticketing.models import TicketCategory

    TicketCategory.objects.filter(pk=6).update(user_creatable=False)
    run = drive(TICKET, bot_world, Chosen({"role": "blue_team", "category": "6", "description": description}))
    assert "ticketing_ticket" not in run.written


@example(points=2**31)
@given(points=st.integers(2**31, 2**53))
def test_resolve_points_past_the_column_are_refused(bot_world: World, points: int) -> None:
    """/tickets resolve took any integer Discord sends (to 2**53) as the points charged, past their integer column
    (DataError); the web's resolve form bounds them by the column, and now the bot reads them with it."""
    drive(RESOLVE, bot_world, Chosen({"role": "admin", "ticket_number": "T001-002", "points": points}))


@example(points="１２")
@given(points=option_text(1, 5))
def test_resolve_modal_points_are_read_as_the_web_reads_them(bot_world: World, points: str) -> None:
    """The resolve modal read its points with int(), unlike the web form (and a NUL in its notes reached the
    database): both now go through the web's resolve form."""
    drive(RESOLVE_MODAL, bot_world, Chosen({"role": "admin", "Resolution Notes": "Fixed\x00", "Points": points}))


@pytest.mark.parametrize("name", ["resolve", "cancel", "change-category", "reassign", "reopen"])
def test_a_ticket_number_option_is_as_long_as_the_column(name: str) -> None:
    """The ticket number options took up to Discord's 6000 characters, and "Ticket ... not found" repeated all of
    them, past the 2000 a message takes: Discord now refuses a number longer than the column."""
    from ticketing.models import Ticket

    command = next(c for c in AdminTicketsCog.tickets_group.walk_commands() if c.name == name)
    param = next(p for p in command.parameters if p.name == "ticket_number")
    assert param.max_value == Ticket._meta.get_field("ticket_number").max_length


ACTIVATE = Command(AdminTeamsCog, AdminTeamsCog.admin_activate_teams.callback, may("admin"))
SET_APPS = Command(AdminCompetitionCog, AdminCompetitionCog.admin_competition_set_apps.callback, may("admin"))
REOPEN = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_reopen.callback, may("ticketing_admin"))


@example(teams="1," + "x" * 3000)
@given(teams=option_text(2000, 6000))
def test_a_team_range_error_repeats_only_the_start_of_the_range(bot_world: World, teams: str) -> None:
    """parse_team_range's error repeated the whole part it couldn't read, and the reply went past 2000 characters."""
    drive(ACTIVATE, bot_world, Chosen({"role": "admin", "teams": teams}))


@example(app_slugs="netbird," + "x" * 3000)
@given(app_slugs=option_text(2000, 6000))
def test_app_slugs_are_read_as_the_competition_page_reads_them(bot_world: World, app_slugs: str) -> None:
    """/competition set-apps stored any text as application slugs (the competition page takes only slugs) and
    repeated them all in its reply, past 2000 characters."""
    drive(SET_APPS, bot_world, Chosen({"role": "admin", "app_slugs": app_slugs}))


@example(reason="r" * 3000)
@given(reason=option_text(2000, 6000))
def test_a_reopen_reply_repeats_only_the_start_of_the_reason(bot_world: World, reason: str) -> None:
    """The reopen reply repeated the whole reason, past the 2000 characters a message takes."""
    drive(REOPEN, bot_world, Chosen({"role": "admin", "ticket_number": "T001-003", "reason": reason}))


@example(target="announcements", message="m" * 3000)
@example(target="1", message="m" * 3000)
@given(target=st.sampled_from(("announcements", "1", "all-teams")), message=option_text(1900, 6000))
def test_a_long_broadcast_is_sent_in_parts(bot_world: World, target: str, message: str) -> None:
    """A broadcast over 2000 characters (with its header) went out as one message, which Discord refuses."""
    drive(BROADCAST, bot_world, Chosen({"role": "admin", "target": target, "message": message}))


@example(target="1", message="m" * 3000)
@given(target=st.sampled_from(("announcements", "1")), message=option_text(1900, 6000))
def test_a_long_web_broadcast_is_sent_in_parts(bot_world: World, target: str, message: str) -> None:
    """The queue sent a broadcast from the portal as one message too, and Discord refused one over 2000."""
    from asgiref.sync import async_to_sync

    from bot.discord_queue import DiscordQueueProcessor
    from core.discord_tasks import BroadcastMessage

    with rolled_back(), discord_world(bot_world, confirm=True) as fakes:
        processor = DiscordQueueProcessor(fakes.bot)
        async_to_sync(processor._handle_broadcast_message)(BroadcastMessage(target=target, message=message, sender="g"))
        assert fakes.sent
        for sent in fakes.sent:
            assert not sent_problems(sent), sent_problems(sent)
            assert not mention_problems(sent, fakes.bot.allowed_mentions)
