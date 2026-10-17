"""Regression tests for bugs the bot's property tests found, each pinned with the input that showed it."""

from hypothesis import example, given

from bot.cogs.admin_competition import AdminCompetitionCog
from bot.tests.bot_harness import Chosen, Command, World, drive, may, option_text

BROADCAST = Command(AdminCompetitionCog, AdminCompetitionCog.admin_broadcast.callback, may("admin"))


@example(message="@everyone the scoring engine is back")
@given(message=option_text(1, 1000))
def test_an_announcement_pings_only_the_blueteam_role(bot_world: World, message: str) -> None:
    """The bot set no default allowed mentions, so any message it sent pinged everything it named: @everyone in a
    broadcast, and every user an ops log line mentioned."""
    drive(BROADCAST, bot_world, Chosen({"role": "admin", "target": "announcements", "message": message}))
