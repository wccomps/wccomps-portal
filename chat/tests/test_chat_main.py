"""`python -m chat`: runs a runner per configured chat backend together, and refuses to start with none."""

import asyncio
import logging

import pytest
from django.core.exceptions import ImproperlyConfigured

from chat import __main__ as chat_main
from chat.zulip import runner as zulip_runner
from core.heartbeat import CHAT_BACKENDS
from core.task_backends import TaskBackend


@pytest.fixture
def no_zulip(settings):
    settings.ZULIP_SITE = ""
    settings.ZULIP_BOT_EMAIL = ""
    settings.ZULIP_API_KEY = ""


@pytest.fixture
def zulip_only(settings, zulip_configured):
    settings.DISCORD_BOT_TOKEN = ""


@pytest.fixture
def not_started(monkeypatch):
    monkeypatch.setattr("bot.client.PortalBot", lambda: pytest.fail("the Discord runner started"))


@pytest.fixture
def zulip_not_started(monkeypatch):
    async def fail(client: object) -> None:
        pytest.fail("the Zulip runner started")

    monkeypatch.setattr(zulip_runner, "run", fail)


class _Bot:
    """Stands in for PortalBot: start runs until cancelled (as a connected bot does), unless told to crash."""

    def __init__(self, crash: bool = False) -> None:
        self.crash = crash
        self.tokens: list[str] = []
        self.closed = False
        self.started = asyncio.Event()

    async def __aenter__(self) -> _Bot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def start(self, token: str) -> None:
        self.tokens.append(token)
        self.started.set()
        if self.crash:
            raise RuntimeError("runner crashed")
        await asyncio.Event().wait()


class _Client:
    """Stands in for ZulipClient, recording what it was built with and whether it was closed."""

    made: list[_Client] = []

    def __init__(self, site: str, email: str, api_key: str) -> None:
        self.credentials = (site, email, api_key)
        self.closed = False
        self.made.append(self)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def client(monkeypatch) -> type[_Client]:
    _Client.made = []
    monkeypatch.setattr(chat_main, "ZulipClient", _Client)
    return _Client


def test_chat_exits_without_backends(settings, no_zulip, not_started, zulip_not_started, caplog):
    settings.DISCORD_BOT_TOKEN = ""

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_:
        chat_main.main()

    assert exit_.value.code == 1
    assert "DISCORD_BOT_TOKEN" in caplog.text and "ZULIP_SITE" in caplog.text


def test_chat_refuses_to_start_without_required_settings(settings, no_zulip, not_started):
    settings.DISCORD_BOT_TOKEN = "token"
    settings.ORG_NAME = ""

    with pytest.raises(ImproperlyConfigured, match="^ORG_NAME not set; the chat process refuses to start"):
        chat_main.main()


def test_chat_refuses_to_start_with_a_plain_http_service_url(settings, no_zulip, not_started):
    settings.DISCORD_BOT_TOKEN = "token"
    settings.NEXTCLOUD_URL = "http://cloud.example.org"

    with pytest.raises(ImproperlyConfigured, match="NEXTCLOUD_URL must be https"):
        chat_main.main()


def test_a_refused_zulip_site_stops_the_process_before_any_runner(
    settings, zulip_configured, not_started, zulip_not_started, caplog
):
    settings.ZULIP_SITE = "http://zulip.example.org"

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_:
        chat_main.main()

    assert exit_.value.code == 1
    assert "ZULIP_SITE" in caplog.text


def test_every_chat_backend_has_a_runner():
    assert set(chat_main.RUNNERS) == {TaskBackend(backend.name) for backend in CHAT_BACKENDS}


def test_discord_only_starts_the_bot_without_a_log_handler_of_its_own(
    settings, no_zulip, zulip_not_started, monkeypatch
):
    """discord.py's logs go through the Django console handler: start(), unlike run(), adds no handler. The bot is
    closed when its runner ends."""
    settings.DISCORD_BOT_TOKEN = "token"
    bot = _Bot(crash=True)
    monkeypatch.setattr("bot.client.PortalBot", lambda: bot)

    with pytest.raises(SystemExit):
        chat_main.main()

    assert bot.tokens == ["token"]
    assert bot.closed


def test_zulip_only_runs_the_zulip_runner_with_a_client_from_settings(zulip_only, not_started, client, monkeypatch):
    ran: list[object] = []

    async def run(zulip_client: object) -> None:
        ran.append(zulip_client)

    monkeypatch.setattr(zulip_runner, "run", run)

    chat_main.main()

    [made] = client.made
    assert ran == [made]
    assert made.credentials == ("https://zulip.example.org", "portal-bot@zulip.example.org", "test-key")
    assert made.closed


def test_both_backends_run_together_and_a_crash_ends_the_chat_process(
    settings, zulip_configured, client, monkeypatch, caplog
):
    """The Zulip runner sees the bot running: one event loop runs both. Its crash stops the bot (closed on the way
    out) and ends the process."""
    bot = _Bot()
    monkeypatch.setattr("bot.client.PortalBot", lambda: bot)

    async def run(zulip_client: object) -> None:
        await asyncio.wait_for(bot.started.wait(), timeout=5)
        raise RuntimeError("runner crashed")

    monkeypatch.setattr(zulip_runner, "run", run)

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_:
        chat_main.main()

    assert exit_.value.code == 1
    assert "runner crashed" in caplog.text
    assert bot.tokens == [settings.DISCORD_BOT_TOKEN]
    assert bot.closed
    assert client.made[0].closed
