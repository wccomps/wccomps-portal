"""`python -m chat`: runs a runner per configured chat backend, and refuses to start with none it can run."""

import asyncio
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
    monkeypatch.setattr(chat_main.asyncio, "run", lambda _coro: pytest.fail("the chat process started"))


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
    started_with: list[str] = []

    async def __aenter__(self) -> _Bot:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pass

    async def start(self, token: str) -> None:
        self.started_with.append(token)


def test_discord_runs_without_a_log_handler_of_its_own(settings, no_zulip, monkeypatch):
    """start(), not run(): discord.py's logs go through the Django console handler and its line-safe formatter."""
    settings.DISCORD_BOT_TOKEN = "token"
    _Bot.started_with = []
    monkeypatch.setattr(chat_main, "PortalBot", _Bot)

    chat_main.main()

    assert _Bot.started_with == ["token"]
    assert all(isinstance(h, logging.NullHandler) for h in logging.getLogger("discord").handlers)


async def test_a_crashing_runner_ends_the_chat_process(monkeypatch):
    """The liveness probe restarts a chat process that exited."""

    async def crash() -> None:
        raise RuntimeError("runner crashed")

    monkeypatch.setattr(chat_main, "run_discord", crash)

    with pytest.raises(ExceptionGroup) as ended:
        await asyncio.wait_for(chat_main.run(), timeout=5)

    assert ended.group_contains(RuntimeError, match="runner crashed")
