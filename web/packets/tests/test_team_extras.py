"""A packet's per-team CSV that the csv module can't read is refused naming the line it gave up on."""

import pytest
from django.core.exceptions import ValidationError

from packets.forms import TEAM_EXTRAS_HELP, parse_team_extras


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ('team,key\nTeam01,a\nTeam02,"' + "x" * 140_000, 3),
        ("team,key\nTeam01," + "y" * 140_000 + "\nTeam02,b", 2),
        ('team,key\nTeam01,"a\n' + ("y" * 70_000 + "\n") * 3, 2),
        ("team," + "k" * 140_000 + "\nTeam01,a", 1),
    ],
)
def test_per_team_data_the_reader_gives_up_on_names_the_line_it_gave_up_on(text, line):
    """A cell past the csv module's field limit (a quote left open) was put on the line before it, the header's on 0."""
    with pytest.raises(ValidationError) as refused:
        parse_team_extras(text)

    assert refused.value.messages == [
        f"The per-team data isn't CSV the upload can read (line {line}). {TEAM_EXTRAS_HELP}"
    ]
