"""Portal text made right for Zulip. Bold, italics, code and links are written the same in Zulip and Discord; a time
in core's free text is in Discord's markup and is converted, and a mention in someone's text must not notify anyone,
as Discord's sends allow none."""

import re
from datetime import UTC, datetime

# <t:1794772800> or <t:1794772800:t>: Discord shows the time in each reader's own timezone, as Zulip's <time:...> does
DISCORD_TIMESTAMP_RE = re.compile(r"<t:(-?\d+)(?::[tTdDfFR])?>")

# @**Name**, @**all**, @*group*: Zulip's mentions; @_**Name** and @_*group* show the same without notifying
MENTION_RE = re.compile(r"@(?=\*)")


def fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def zulip_time(moment: datetime) -> str:
    """The time in each reader's own timezone."""
    return f"<time:{moment.astimezone(UTC).isoformat()}>"


def _converted(match: re.Match[str]) -> str:
    try:
        moment = datetime.fromtimestamp(int(match.group(1)), UTC)
    except OverflowError, ValueError, OSError:
        return match.group()  # past the years a datetime holds: left as written
    return zulip_time(moment)


def to_zulip(text: str) -> str:
    """Text with Discord's time markup converted; convert before measuring it against a limit, as it grows."""
    return DISCORD_TIMESTAMP_RE.sub(_converted, text)


def quiet(text: str) -> str:
    """Someone's text (a comment, a ticket's title, a broadcast) with its mentions made silent."""
    return MENTION_RE.sub("@_", text)
