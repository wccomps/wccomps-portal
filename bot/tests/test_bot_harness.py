"""The bot harness's oracle fails on what Discord would refuse and on mentions that could ping anyone."""

import discord
import pytest

from bot.tests.bot_harness import Chosen, Sent, embed_problems, mention_problems, sent_problems


def test_over_long_content_and_empty_messages_are_refused() -> None:
    assert sent_problems(Sent(where="x", method="send", content="a" * 2001))
    assert sent_problems(Sent(where="x", method="send", content="  "))
    assert not sent_problems(Sent(where="x", method="send", content="a" * 2000))


def test_embed_limits_count_every_part() -> None:
    fields = discord.Embed(title="t")
    for _ in range(6):
        fields.add_field(name="n", value="v" * 1024)
    assert any("in all" in problem for problem in embed_problems([fields]))
    blank = discord.Embed().add_field(name="Name", value=" \u2029")
    assert any("blank" in problem for problem in embed_problems([blank]))
    assert not embed_problems([discord.Embed(title="t" * 256, description="d" * 4096)])


def test_mentions_are_limited_by_the_send_over_the_clients_default() -> None:
    none = discord.AllowedMentions.none()
    sent = Sent(where="x", method="send", content="@everyone")
    assert mention_problems(sent, None) == ["@everyone and @here", "every user named", "every role named"]
    assert mention_problems(sent, none) == []
    sent.allowed_mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(1)])
    assert mention_problems(sent, none) == []
    sent.allowed_mentions = discord.AllowedMentions(roles=True)
    assert "every role named" in mention_problems(sent, none)


def test_chosen_values_must_be_chosen() -> None:
    chosen = Chosen({"role": "admin"})
    assert chosen.draw(None, label="role") == "admin"  # type: ignore[arg-type]
    assert chosen.draw(None, label="omit notes") is True  # type: ignore[arg-type]
    with pytest.raises(KeyError):
        chosen.draw(None, label="category")  # type: ignore[arg-type]
