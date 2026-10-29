"""The error webhook embed: request text shows as inert text within Discord limits, and a refused alert is logged."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth.models import AnonymousUser

from core.discord_logging import DiscordWebhookHandler

LINK = "[Sign in again](https://attacker.example/login)"
LIVE = "https://attacker.example/x <@123> <@&456> <#789> <t:1700000000> @everyone"


def _embed(record: logging.LogRecord, response: httpx.Response | None = None) -> dict:
    with patch.dict("os.environ", {"DISCORD_ERROR_WEBHOOK_URL": "https://discord.example/api/webhooks/1/x"}):
        handler = DiscordWebhookHandler()
    with patch("core.discord_logging.httpx.post", return_value=response or httpx.Response(204)) as post:
        handler.emit(record)
    return post.call_args.kwargs["json"]["embeds"][0]


def _record(message: str, args: tuple, path: str = "/", error: str = f"bad value ```{LINK}") -> logging.LogRecord:
    try:
        raise ValueError(error)
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    record = logging.LogRecord("wccomps.errors", logging.ERROR, __file__, 1, message, args, exc_info)
    user = MagicMock(is_authenticated=True, username=f"user`{LIVE}")
    record.request = SimpleNamespace(method="GET", path=path, user=user)
    return record


def _field(embed: dict, name: str) -> str:
    (value,) = [f["value"] for f in embed["fields"] if f["name"] == name]
    return value


def _inert(value: str) -> bool:
    """One inline code span with no backtick inside it: Discord renders nothing in it."""
    return len(value) > 2 and value[0] == value[-1] == "`" and "`" not in value[1:-1]


def test_request_text_is_inert_wherever_it_lands() -> None:
    embed = _embed(_record('"GET %s" 500', (f"/x{LINK}{LIVE}",), path=f"/x{LINK}{LIVE}"))

    for name in ("Request", "User", "Message"):
        assert _inert(_field(embed, name)), name
    assert "attacker" not in embed["title"]


def test_an_anonymous_request_has_no_user_field() -> None:
    record = _record("failed", ())
    record.request.user = AnonymousUser()

    assert "User" not in [f["name"] for f in _embed(record)["fields"]]


def test_a_long_markdown_heavy_path_stays_within_discord_limits() -> None:
    path = "/" + "-_*" * 2000
    embed = _embed(_record("%s", (path,), path=path, error="-_*" * 2000))

    assert len(embed["title"]) <= 256
    for field in embed["fields"]:
        assert len(field["value"]) <= 1024, field["name"]
    assert _inert(_field(embed, "Request"))


def test_no_run_of_backticks_closes_the_traceback_block() -> None:
    for run in ("`" * 5, "`" * 6):
        traceback = _field(_embed(_record("failed", (), error=f"bad {run} {LINK}")), "Traceback")

        assert traceback.startswith("```\n")
        assert traceback.endswith("\n```")
        assert "`" not in traceback[3:-3], run


def test_a_refused_alert_is_logged_on_the_console(caplog) -> None:
    with caplog.at_level("WARNING", logger="core.discord_logging"):
        _embed(_record("failed", ()), httpx.Response(400, text="Invalid Form Body"))

    assert any("400" in r.getMessage() for r in caplog.records if r.name == "core.discord_logging")


def test_bidi_and_format_characters_show_escaped() -> None:
    path = "/a\u202eb\u2066\u2069\u200b\u00ad"
    embed = _embed(_record("%s", (path,), path=path))

    for name in ("Request", "Message"):
        assert _field(embed, name).isprintable(), name


def test_a_traceback_shows_bidi_and_control_characters_escaped_and_keeps_its_lines() -> None:
    error = "bad \u202e\u2066\u200b\x1b[31m" + "\u202e" * 2000
    traceback = _field(_embed(_record("failed", (), error=error)), "Traceback")

    assert len(traceback) <= 1024
    lines = traceback.split("\n")
    assert len(lines) > 3
    assert all(line.isprintable() for line in lines)
