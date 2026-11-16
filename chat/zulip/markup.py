"""Portal text written for Discord, made right for Zulip. Bold, italics, code and links are written the same in both;
Discord's timestamps differ, and a mention in someone's text must not notify anyone, as Discord's sends allow none."""

import re
from datetime import UTC, datetime

# <t:1794772800> or <t:1794772800:t>: Discord shows the time in each reader's own timezone, as Zulip's <time:...> does
DISCORD_TIMESTAMP_RE = re.compile(r"<t:(-?\d+)(?::[tTdDfFR])?>")

# @**Name**, @**all**, @*group*: Zulip's mentions; @_**Name** and @_*group* show the same without notifying
MENTION_RE = re.compile(r"@(?=\*)")


def _zulip_time(match: re.Match[str]) -> str:
    try:
        moment = datetime.fromtimestamp(int(match.group(1)), UTC)
    except OverflowError, ValueError, OSError:
        return match.group()  # past the years a datetime holds: left as written
    return f"<time:{moment.isoformat()}>"


def to_zulip(text: str) -> str:
    return DISCORD_TIMESTAMP_RE.sub(_zulip_time, text)


def quiet(text: str) -> str:
    """Someone's text (a comment, a ticket's title, a broadcast) with its mentions made silent."""
    return MENTION_RE.sub("@_", text)
