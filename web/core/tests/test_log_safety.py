"""The app's log lines: request text escaped for a quoted field, and one line per message on the console."""

import logging

import pytest

from core.log_safety import escape_log_text, escape_log_word


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        ("a\nb", "a\\nb"),
        ("a\\nb", "a\\\\nb"),
        ('say "hi"', 'say \\"hi\\"'),
        ("\r\x1b\x7f\x85", "\\r\\x1b\\x7f\\x85"),
        ("\u2028\u2029", "\\u2028\\u2029"),
        ("\u202e\u2066\u2069\u200b\u00ad", "\\u202e\\u2066\\u2069\\u200b\\xad"),
        ("café 東京", "café 東京"),
    ],
)
def test_escape_log_text(text: str, escaped: str) -> None:
    assert escape_log_text(text) == escaped


def test_an_unquoted_field_escapes_whitespace_too() -> None:
    assert escape_log_word('h 200 "x\u00a0y\u3000z\n') == 'h\\x20200\\x20\\"x\\xa0y\\u3000z\\n'


def _console_formatter() -> logging.Formatter:
    """The formatter LOGGING gives the console handler, which every logger writes through."""
    from django.conf import settings
    from django.utils.module_loading import import_string

    spec = settings.LOGGING["formatters"]["verbose"]
    return import_string(spec.get("class", "logging.Formatter"))(spec["format"], style=spec["style"])


def test_a_console_line_stays_one_line_whatever_its_message_holds() -> None:
    record = logging.LogRecord("any", logging.WARNING, __file__, 1, "%s", ("a\nWARNING forged\u2028b",), None)

    line = _console_formatter().format(record)

    assert len(line.splitlines()) == 1
    assert "a\\nWARNING forged\\u2028b" in line


def test_a_console_traceback_keeps_its_lines() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()
    record = logging.LogRecord("any", logging.ERROR, __file__, 1, "failed", (), exc_info)

    lines = _console_formatter().format(record).splitlines()

    assert lines[1] == "Traceback (most recent call last):"
    assert lines[-1] == "RuntimeError: boom"


@pytest.mark.django_db
def test_an_oauth_error_from_the_callback_logs_one_line(caplog) -> None:
    from django.test import Client

    with caplog.at_level("WARNING", logger="core.oauth"):
        Client().get("/auth/callback/", {"error": "x\nWARNING forged", "error_description": "y\r\nz"})

    (record,) = [r for r in caplog.records if r.name == "core.oauth"]
    assert len(_console_formatter().format(record).splitlines()) == 1


def test_a_console_line_shows_bidi_and_format_characters_escaped() -> None:
    record = logging.LogRecord("any", logging.WARNING, __file__, 1, "%s", ("/a\u202eb\u2066\u200b",), None)

    line = _console_formatter().format(record)

    assert line.isprintable()
    assert "/a\\u202eb\\u2066\\u200b" in line
