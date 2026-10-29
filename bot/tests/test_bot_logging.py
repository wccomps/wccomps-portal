"""The bot's discord.py logs go through the Django console handler and its line-safe formatter, not their own."""

import logging
from unittest.mock import patch

import main


def test_the_bot_runs_without_a_log_handler_of_its_own(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    with patch.object(main, "PortalBot") as bot_class:
        main.main()

    (call,) = bot_class.return_value.run.call_args_list
    assert "log_handler" in call.kwargs
    assert call.kwargs["log_handler"] is None


def test_discord_logs_reach_the_root_handlers() -> None:
    from django.conf import settings

    discord_logger = logging.getLogger("discord")
    assert discord_logger.propagate
    assert discord_logger.isEnabledFor(logging.INFO)
    assert all(isinstance(h, logging.NullHandler) for h in discord_logger.handlers)
    assert "console" in settings.LOGGING["root"]["handlers"]
