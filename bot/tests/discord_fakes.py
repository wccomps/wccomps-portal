"""Small Discord stand-ins shared by the bot tests."""

from collections.abc import AsyncIterator, Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
from django.conf import settings

BOT_USER = MagicMock(spec=discord.ClientUser, id=990099)


def competition_thread(**attributes: Any) -> MagicMock:
    """A thread in the competition guild, as ticket_thread accepts it; settings are read when it is made."""
    thread = MagicMock(spec=discord.Thread, **attributes)
    thread.guild.id = settings.COMPETITION_GUILD_ID
    return thread


async def _each(items: Iterable[Any]) -> AsyncIterator[Any]:
    for item in list(items):
        yield item


def ticket_message(ticket_number: str) -> str:
    """The text of the bot's ticket message in a ticket's thread."""
    return f"**Ticket #{ticket_number}**: use the buttons below to manage this ticket."


def recording_thread(thread_id: int, *, failing_sends: Iterable[int] = (), pinned_text: str | None = None) -> MagicMock:
    """A competition thread that keeps what the bot sends to it (in .messages, texts in .texts) and what it pins,
    so pins() and history() show them; the sends numbered in failing_sends (from 1) raise instead. pinned_text is
    a message of the bot's already pinned there (not in .texts)."""
    thread = competition_thread(id=thread_id, archived=False, locked=False)
    thread.messages, thread.texts, pinned = [], [], []
    failing, attempts = set(failing_sends), [0]

    async def send(content: str | None = None, **_kwargs: Any) -> MagicMock:
        attempts[0] += 1
        if attempts[0] in failing:
            raise discord.HTTPException(MagicMock(status=500), "Internal Server Error")
        message = MagicMock(spec=discord.Message, id=thread_id * 100 + len(thread.messages), content=content)
        message.author = BOT_USER
        message.pin = AsyncMock(side_effect=lambda: pinned.append(message))
        thread.messages.append(message)
        thread.texts.append(content)
        return message

    thread.send = AsyncMock(side_effect=send)
    if pinned_text is not None:
        pinned.append(MagicMock(spec=discord.Message, id=thread_id * 100 + 99, content=pinned_text, author=BOT_USER))
    thread.pins = MagicMock(side_effect=lambda **_kwargs: _each(pinned))
    thread.history = MagicMock(side_effect=lambda **_kwargs: _each(thread.messages))
    thread.add_user = AsyncMock()
    thread.delete = AsyncMock()
    return thread


def bot_with(*threads: MagicMock) -> MagicMock:
    """A bot that finds these threads by ID, and no other."""
    by_id = {t.id: t for t in threads}
    bot = MagicMock(spec=discord.Client)
    bot.user = BOT_USER
    bot.unified_dashboard = MagicMock()
    bot.get_channel.side_effect = by_id.get
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "Unknown Channel"))
    return bot
