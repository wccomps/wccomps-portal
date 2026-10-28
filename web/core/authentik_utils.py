import csv
import io
import logging
import secrets
from collections.abc import Iterable
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


def _record_password(team_number: int, password: str) -> None:
    """Keep the team's password on its school info, for packet emails; a team with no school info has no copy."""
    from team.models import SchoolInfo

    SchoolInfo.objects.filter(team__team_number=team_number).update(password=password)


def reset_team_password(team_number: int) -> tuple[str | None, str]:
    """Give a team account a new random password and record it on the team's school info for packet emails.

    Returns (password, "") on success, (None, error) otherwise.
    """
    from .authentik_manager import AuthentikManager

    password = generate_blueteam_password()
    manager = AuthentikManager()
    try:
        success, error = manager.reset_blueteam_password(team_number, password)
    finally:
        manager.close()
    if not success:
        return None, error
    _record_password(team_number, password)
    return password, ""


@dataclass(frozen=True)
class CredentialReset:
    """What resetting a team's credentials did: the new password (None when the reset failed, with error saying
    why), and why the account's sessions weren't ended (None when they were, or when nothing was tried)."""

    password: str | None
    error: str = ""
    sessions_error: str | None = None

    @property
    def sessions_ended(self) -> bool:
        return self.password is not None and self.sessions_error is None

    @property
    def sessions_outcome(self) -> str:
        return f"could not end its sessions: {self.sessions_error}" if self.sessions_error else "ended its sessions"

    def audit_details(self) -> dict[str, object]:
        return {
            "password_reset": self.password is not None,
            "sessions_revoked": self.sessions_ended,
            "sessions_error": self.sessions_error,
        }


def reset_team_credentials(team_number: int) -> CredentialReset:
    """Give a team account a new random password, then end the account's sessions."""
    from .authentik_manager import AuthentikManager

    password = generate_blueteam_password()
    manager = AuthentikManager()
    try:
        reset_error, sessions_error = manager.reset_team_credentials(team_number, password)
    finally:
        manager.close()
    if reset_error is not None:
        return CredentialReset(password=None, error=reset_error)
    _record_password(team_number, password)
    return CredentialReset(password=password, sessions_error=sessions_error)


@dataclass(frozen=True)
class BulkPasswordResetResult:
    password_list: list[tuple[int, str, str]]
    failed_resets: list[tuple[str, str]]
    sessions_failed: list[tuple[str, str]]

    def audit_details(self) -> dict[str, object]:
        return {
            "success_count": len(self.password_list),
            "failed_count": len(self.failed_resets),
            "sessions_failed": [f"{username}: {why}" for username, why in self.sessions_failed],
        }


def sessions_failure_summary(sessions_failed: list[tuple[str, str]]) -> str:
    """'could not end sessions for team01, team02: <why>', one clause per distinct reason."""
    by_reason: dict[str, list[str]] = {}
    for username, reason in sessions_failed:
        by_reason.setdefault(reason, []).append(username)
    return "; ".join(f"could not end sessions for {', '.join(names)}: {why}" for why, names in by_reason.items())


def reset_teams_credentials(team_numbers: Iterable[int]) -> BulkPasswordResetResult:
    """Reset passwords and end sessions for multiple teams."""
    password_list: list[tuple[int, str, str]] = []
    failed_resets: list[tuple[str, str]] = []
    sessions_failed: list[tuple[str, str]] = []

    for team_num in team_numbers:
        username = team_username(team_num)
        reset = reset_team_credentials(team_num)
        if reset.password is None:
            failed_resets.append((username, reset.error))
            continue
        password_list.append((team_num, username, reset.password))
        if reset.sessions_error:
            sessions_failed.append((username, reset.sessions_error))

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
