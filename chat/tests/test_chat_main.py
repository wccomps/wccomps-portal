"""`python -m chat`: runs a runner per configured chat backend, and refuses to start with none it can run."""

import logging

import pytest
from django.core.exceptions import ImproperlyConfigured

from chat import __main__ as chat_main


@pytest.fixture
def no_zulip(settings):
    settings.ZULIP_SITE = ""
    settings.ZULIP_BOT_EMAIL = ""
    settings.ZULIP_API_KEY = ""


@pytest.fixture
def not_started(monkeypatch):
    monkeypatch.setattr(chat_main, "PortalBot", lambda: pytest.fail("the chat process started"))


def test_chat_exits_without_backends(settings, no_zulip, not_started):
    settings.DISCORD_BOT_TOKEN = ""

    with pytest.raises(SystemExit) as exit_:
        chat_main.main()

    assert exit_.value.code == 1


@pytest.mark.parametrize("discord_token", ["", "token"])
def test_chat_refuses_zulip_until_it_has_a_runner(settings, not_started, caplog, discord_token):
    settings.DISCORD_BOT_TOKEN = discord_token
    settings.ZULIP_SITE = "https://chat.example.com"
    settings.ZULIP_BOT_EMAIL = "portal-bot@example.com"
    settings.ZULIP_API_KEY = "key"

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_:
        chat_main.main()

    assert exit_.value.code == 1
    assert any("Zulip" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


def test_chat_refuses_to_start_without_required_settings(settings, no_zulip, not_started, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "token"
    monkeypatch.setattr(chat_main, "missing_settings", lambda: ["ORG_NAME"])

    with pytest.raises(ImproperlyConfigured, match="ORG_NAME"):
        chat_main.main()


class _Bot:
    runs: list[tuple[str, object]] = []

    def run(self, token: str, *, log_handler: object) -> None:
        self.runs.append((token, log_handler))


def test_discord_runs_without_a_log_handler_of_its_own(settings, no_zulip, monkeypatch):
    """discord.py's logs go through the Django console handler and its line-safe formatter."""
    settings.DISCORD_BOT_TOKEN = "token"
    _Bot.runs = []
    monkeypatch.setattr(chat_main, "PortalBot", _Bot)

    chat_main.main()

    assert _Bot.runs == [("token", None)]


def test_a_crashing_runner_ends_the_chat_process(settings, no_zulip, monkeypatch, caplog):
    """The process exits, and its container restarts."""
    settings.DISCORD_BOT_TOKEN = "token"

    class Crashing:
        def run(self, token: str, *, log_handler: object) -> None:
            raise RuntimeError("runner crashed")

    monkeypatch.setattr(chat_main, "PortalBot", Crashing)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_:
        chat_main.main()

    assert exit_.value.code == 1
    assert "runner crashed" in caplog.text
