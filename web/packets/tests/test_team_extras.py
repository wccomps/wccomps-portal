"""A packet's per-team CSV that the csv module can't read is refused naming the line its bad row starts on."""

import pytest
from django.core.exceptions import ValidationError

from packets.forms import TEAM_EXTRAS_HELP, parse_team_extras


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ('team,key\nTeam01,a\nTeam02,"' + "x" * 140_000, 3),
        ("team,key\nTeam01," + "y" * 140_000 + "\nTeam02,b", 2),
        ('team,key\nTeam01,"a\n' + ("y" * 70_000 + "\n") * 3, 2),
        ('team,note,key\nTeam01,"first\nsecond","' + "x" * 140_000, 2),
        ("team," + "k" * 140_000 + "\nTeam01,a", 1),
        ('team,key\nTeam01,a\n\n\nTeam02,"' + "x" * 140_000, 5),
        ('team,key\r\nTeam01,a\r\n\r\nTeam02,"' + "x" * 140_000, 4),
        ("\n \r\n\t\r  team," + "k" * 140_000, 4),
        ("\u2028\r\n  team\nTeam01," + "x" * 140_000, 3),
    ],
)
def test_per_team_data_the_reader_gives_up_on_names_the_line_its_bad_row_starts_on(text: str, line: int) -> None:
    """A row past the csv module's field limit (a quote left open, an oversized header) names the line it starts on."""
    with pytest.raises(ValidationError) as refused:
        parse_team_extras(text)

    assert refused.value.messages == [
        f"The per-team data isn't CSV the upload can read (line {line}). {TEAM_EXTRAS_HELP}"
    ]
