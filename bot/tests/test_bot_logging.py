"""The bot's discord.py logs go through the Django console handler and its line-safe formatter, not their own
(chat/tests/test_chat_main.py checks the chat process starts the bot without one)."""

import logging


def test_discord_logs_reach_the_root_handlers() -> None:
    from django.conf import settings

    discord_logger = logging.getLogger("discord")
    assert discord_logger.propagate
    assert discord_logger.isEnabledFor(logging.INFO)
    assert all(isinstance(h, logging.NullHandler) for h in discord_logger.handlers)
    assert "console" in settings.LOGGING["root"]["handlers"]
