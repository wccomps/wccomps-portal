"""Escaping for request text in log lines. No Django imports: gunicorn.conf.py loads it before Django is set up."""

import logging
from collections.abc import Callable


def _escape_char(char: str) -> str:
    if char in '"\\':
        return "\\" + char
    escaped = char.encode("unicode_escape").decode("ascii")
    return escaped if escaped != char else f"\\x{ord(char):02x}"


def _escape(text: str, also: Callable[[str], bool]) -> str:
    # Unprintable (controls, bidi and zero-width format characters, separators) can end, drive or disguise a line
    return "".join(_escape_char(c) if not c.isprintable() or also(c) else c for c in text)


def escape_unprintable(text: str) -> str:
    return text if text.isprintable() else _escape(text, lambda c: False)


def escape_log_text(text: str) -> str:
    """text for a quoted log field: unprintable characters, and the quote and backslash that delimit and escape the
    field, backslash-escaped; the rest as is."""
    return _escape(text, lambda c: c in '"\\')


def escape_log_word(text: str) -> str:
    """text for an unquoted log field: escape_log_text's escapes, and whitespace too."""
    return _escape(text, lambda c: c in '"\\' or c.isspace())


class LineSafeFormatter(logging.Formatter):
    """Keeps each record's message on one line and shown as written; a traceback keeps its own lines."""

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 - logging.Formatter's name
        return escape_unprintable(super().formatMessage(record))
