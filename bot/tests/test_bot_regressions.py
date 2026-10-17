"""Regression tests for bugs the bot's property tests found, each pinned with the input that showed it."""

from hypothesis import example, given

from bot.cogs.admin_competition import AdminCompetitionCog
from bot.cogs.admin_tickets import AdminTicketsCog
from bot.tests.bot_harness import Chosen, Command, World, drive, may, option_text

BROADCAST = Command(AdminCompetitionCog, AdminCompetitionCog.admin_broadcast.callback, may("admin"))
RESOLVE = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_resolve.callback, may("ticketing_support"))
REASSIGN = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_reassign.callback, may("ticketing_admin"))


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
