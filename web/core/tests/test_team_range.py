"""The team range the bulk team actions, the password reset and the bot's team commands read ("1,3,5-10")."""

import pytest

from core.authentik_utils import parse_team_range


@pytest.mark.parametrize(
    ("text", "teams"),
    [
        ("1,,3", [1, 3]),
        ("4, ,2", [2, 4]),
        ("50", [50]),
        ("1-50", list(range(1, 51))),
        ("5-5", [5]),
        ("1-3", [1, 2, 3]),
        ("48-50,1", [1, 48, 49, 50]),
    ],
)
def test_a_range_gives_its_teams(text, teams):
    assert parse_team_range(text) == teams


LONG = "x" * 40


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("0", "Team number must be 1-50, got: 0"),
        ("51", "Team number must be 1-50, got: 51"),
        ("1" * 40, f"Team number must be 1-50, got: {'1' * 30}..."),
        ("0-5", "Team numbers must be 1-50, got: 0-5"),
        ("45-51", "Team numbers must be 1-50, got: 45-51"),
        ("0-51", "Team numbers must be 1-50, got: 0-51"),
        ("1-" + "0" * 40 + "51", f"Team numbers must be 1-50, got: 1-{'0' * 28}..."),
        ("6-5", "Invalid range: 6-5 (start > end)"),
        ("50-" + "0" * 40 + "1", f"Invalid range: 50-{'0' * 27}... (start > end)"),
        ("a-b", "Invalid range format: a-b"),
        ("abc-" + LONG, f"Invalid range format: abc-{'x' * 26}..."),
        (LONG, f"Invalid team number: {'x' * 30}..."),
        (" , ", "No teams specified"),
    ],
)
def test_a_range_outside_the_teams_or_unreadable_is_refused_saying_why(text, error):
    """The error is the bot's reply and the form's field error; a long part is cut to its start."""
    with pytest.raises(ValueError) as refused:
        parse_team_range(text)
    assert str(refused.value) == error
