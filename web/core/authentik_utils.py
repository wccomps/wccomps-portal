import csv
import io
import logging
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from team.models import MAX_TEAMS, team_username

from .authentik_manager import AuthentikUser
from .utils import shorten

logger = logging.getLogger(__name__)


def validate_team_account(user_data: AuthentikUser, expected_username: str) -> tuple[bool, str]:
    """Validate that a user account is a legitimate team account."""
    retrieved_username = user_data.get("username", "")

    if not retrieved_username.startswith("team"):
        return (
            False,
            f"Security error: User {retrieved_username} is not a team account",
        )

    if retrieved_username != expected_username:
        return (
            False,
            f"Security error: Username mismatch (expected {expected_username}, got {retrieved_username})",
        )

    return (True, "")


def reset_team_password(team_number: int) -> tuple[str | None, str]:
    """Give a team account a new random password and record it on the team's school info for packet emails.

    A team with no school info gets the new password in Authentik only. Returns (password, "") on success,
    (None, error) otherwise.
    """
    from team.models import SchoolInfo

    from .authentik_manager import AuthentikManager

    password = generate_blueteam_password()
    success, error = AuthentikManager().reset_blueteam_password(team_number, password)
    if not success:
        return None, error
    SchoolInfo.objects.filter(team__team_number=team_number).update(password=password)
    return password, ""


def reset_team_credentials(team_number: int) -> tuple[str | None, str, bool]:
    """Reset a team account's password, then ask Authentik to end the account's sessions.

    Returns (password, error, sessions_revoked); password is None when the reset failed.
    """
    from .authentik_manager import AuthentikManager

    password, error = reset_team_password(team_number)
    if not password:
        return None, error, False
    revoked, _, _ = AuthentikManager().revoke_user_sessions(team_username(team_number))
    return password, "", revoked


@dataclass(frozen=True)
class BulkPasswordResetResult:
    password_list: list[tuple[int, str, str]]
    failed_resets: list[tuple[str, str]]
    sessions_failed: list[str]


def reset_teams_credentials(
    team_numbers: Iterable[int],
    reset_fn: Callable[[int], tuple[str | None, str, bool]] = reset_team_credentials,
) -> BulkPasswordResetResult:
    """Reset passwords and revoke sessions for multiple teams."""
    password_list: list[tuple[int, str, str]] = []
    failed_resets: list[tuple[str, str]] = []
    sessions_failed: list[str] = []

    for team_num in team_numbers:
        username = team_username(team_num)
        password, error, sessions_revoked = reset_fn(team_num)
        if password:
            password_list.append((team_num, username, password))
            if not sessions_revoked:
                sessions_failed.append(username)
        else:
            failed_resets.append((username, error))

    return BulkPasswordResetResult(
        password_list=password_list,
        failed_resets=failed_resets,
        sessions_failed=sessions_failed,
    )


def generate_blueteam_password() -> str:
    """Generate a readable blue team password like "Correct-Horse-742!" or "Battery-@199-Staple"."""
    from xkcdpass import xkcd_password as xp

    # Get EFF long wordlist (7,776 words)
    wordlist = xp.generate_wordlist(wordfile=xp.locate_wordfile())

    words = xp.generate_xkcdpassword(wordlist, numwords=2, delimiter="-", case="capitalize")

    number = secrets.randbelow(900) + 100

    special_chars = "!@#$%&*+"
    special_char = secrets.choice(special_chars)

    insert_value = f"{number}{special_char}" if secrets.choice([True, False]) else f"{special_char}{number}"

    position = secrets.randbelow(3)

    word_parts = words.split("-")
    if position == 0:
        result = f"{insert_value}-{words}"
    elif position == 1:
        result = f"{word_parts[0]}-{insert_value}-{word_parts[1]}"
    else:  # position == 2
        result = f"{words}-{insert_value}"

    return result


def parse_team_range(range_str: str) -> list[int]:
    """Parse a team range string like "1,3,5-10,15" into sorted unique team numbers."""
    team_numbers: set[int] = set()

    for raw_part in range_str.split(","):
        part = raw_part.strip()
        if not part:
            continue

        if "-" in part:
            try:
                start_str, end_str = part.split("-", 1)
                start_num = int(start_str.strip())
                end_num = int(end_str.strip())
            except ValueError as e:
                raise ValueError(f"Invalid range format: {shorten(part, 30)}") from e

            if start_num > end_num:
                raise ValueError(f"Invalid range: {shorten(part, 30)} (start > end)")
            if start_num < 1 or end_num > MAX_TEAMS:
                raise ValueError(f"Team numbers must be 1-{MAX_TEAMS}, got: {shorten(part, 30)}")

            team_numbers.update(range(start_num, end_num + 1))
        else:
            try:
                num = int(part)
            except ValueError as e:
                raise ValueError(f"Invalid team number: {shorten(part, 30)}") from e

            if num < 1 or num > MAX_TEAMS:
                raise ValueError(f"Team number must be 1-{MAX_TEAMS}, got: {shorten(str(num), 30)}")
            team_numbers.add(num)

    return sorted(team_numbers)


def credentials_csv(accounts: Iterable[tuple[str, str]]) -> str:
    """A Username,Password CSV of team accounts' new passwords.

    A plain csv.writer, not core.csv_export: the cells are generated, not user text, and a password such as
    "@123-Apple-Banana" must read back exactly, not with a formula guard's leading quote.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Username", "Password"])
    writer.writerows(accounts)
    return out.getvalue()
