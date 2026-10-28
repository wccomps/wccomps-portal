import csv
import io
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from team.models import MAX_TEAMS, SchoolInfo, team_username

from .authentik_manager import SESSIONS_HIDDEN, AuthentikAPIError, AuthentikManager, AuthentikUser, reason_for
from .discord_tasks import LogToChannel
from .models import AuditLog, DiscordTask
from .utils import shorten

logger = logging.getLogger(__name__)


def _team_user(manager: AuthentikManager, team_number: int) -> AuthentikUser | str:
    """The team's Authentik account, or why not. Raises AuthentikAPIError or httpx.HTTPError when Authentik can't be
    asked."""
    if team_number < 1 or team_number > MAX_TEAMS:
        return f"Team number must be between 1 and {MAX_TEAMS}"
    username = team_username(team_number)
    return manager.find_user(username) or f"User {username} not found"


def set_team_account_active(manager: AuthentikManager, team_number: int, is_active: bool) -> tuple[bool, str]:
    try:
        user = _team_user(manager, team_number)
        if isinstance(user, str):
            return False, user
        manager.set_user_active(user, is_active)
    except (AuthentikAPIError, httpx.HTTPError) as e:
        logger.warning(f"Failed to set team {team_number} active={is_active}: {e}")
        return False, reason_for(e)
    return True, ""


@dataclass(frozen=True)
class CredentialReset:
    """The new password, or None and error; sessions_error is None when its Authentik sessions ended or weren't tried.
    sessions_found: the count before the password change, None if unknown; sessions_listed_after: 0 if unlisted."""

    password: str | None
    error: str = ""
    sessions_error: str | None = None
    sessions_found: int | None = None
    sessions_listed_after: int = 0

    @property
    def sessions_outcome(self) -> str:
        if self.sessions_error:
            return f"could not end the team's Authentik sessions: {self.sessions_error}"
        if self.sessions_found is None:
            return "ended the team's Authentik sessions, but could not count them first"
        return "ended the team's Authentik sessions"


def _count_sessions(manager: AuthentikManager, user: AuthentikUser) -> tuple[int | None, bool | None]:
    """(session count or None; whether the token sees no sessions at all, None when the count failed)."""
    try:
        session_ids = manager.user_sessions(user)
        if not session_ids and not manager.sees_any_sessions():
            return None, True
    except (AuthentikAPIError, httpx.HTTPError) as e:
        logger.warning(f"Counting the sessions of {user['username']} failed: {e}")
        return None, None
    return len(session_ids), False


def _end_sessions(manager: AuthentikManager, user: AuthentikUser, hidden: bool | None) -> tuple[int, str | None]:
    """(sessions listed, None when they ended, else why not)."""
    found = 0
    try:
        session_ids = manager.user_sessions(user)
        found = len(session_ids)
        # Recheck only if the count failed: the password change may have ended the only session the token saw
        if not session_ids and (not manager.sees_any_sessions() if hidden is None else hidden):
            return 0, SESSIONS_HIDDEN
        return found, manager.end_sessions(session_ids)
    except (AuthentikAPIError, httpx.HTTPError) as e:
        logger.warning(f"Ending the sessions of {user['username']} failed: {e}")
        return found, reason_for(e)


def _new_password(manager: AuthentikManager, user: AuthentikUser, team_number: int) -> tuple[str | None, str]:
    """(password, "") or (None, why not). Leaves is_active alone: team accounts are enabled only while the competition
    runs."""
    password = generate_blueteam_password()
    try:
        manager.set_password(user, password)
    except (AuthentikAPIError, httpx.HTTPError) as e:
        logger.warning(f"Failed to set the password of team {team_number}: {e}")
        return None, reason_for(e)
    # The copy packet emails send; a team with no school info has none
    SchoolInfo.objects.filter(team__team_number=team_number).update(password=password)
    return password, ""


def set_team_password(team_number: int, actor: str) -> tuple[str | None, str]:
    """A new random password for the team account, leaving its sessions alone; audited."""
    with AuthentikManager() as manager:
        try:
            user = _team_user(manager, team_number)
        except (AuthentikAPIError, httpx.HTTPError) as e:
            user = reason_for(e)
        password, error = (None, user) if isinstance(user, str) else _new_password(manager, user, team_number)
    AuditLog.objects.create(
        action="team_password_set",
        admin_user=actor,
        target_entity="team",
        target_id=team_number,
        details={"password_set": password is not None, "error": error or None},
    )
    return password, error


def _reset_credentials(manager: AuthentikManager, team_number: int) -> CredentialReset:
    try:
        user = _team_user(manager, team_number)
    except (AuthentikAPIError, httpx.HTTPError) as e:
        return CredentialReset(password=None, error=reason_for(e))
    if isinstance(user, str):
        return CredentialReset(password=None, error=user)
    # Before the password changes: Authentik may end sessions itself when it does, leaving nothing to count
    found, hidden = _count_sessions(manager, user)
    password, error = _new_password(manager, user, team_number)
    if password is None:
        return CredentialReset(password=None, error=error, sessions_found=found)
    listed_after, sessions_error = _end_sessions(manager, user, hidden)
    return CredentialReset(
        password=password, sessions_error=sessions_error, sessions_found=found, sessions_listed_after=listed_after
    )


def reset_team_credentials(team_number: int, actor: str) -> CredentialReset:
    """Give a team account a new random password and end its Authentik sessions; audit it and tell ops."""
    with AuthentikManager() as manager:
        reset = _reset_credentials(manager, team_number)
    AuditLog.objects.create(
        action="team_password_reset",
        admin_user=actor,
        target_entity="team",
        target_id=team_number,
        details={
            "password_reset": reset.password is not None,
            "sessions_revoked": reset.password is not None and reset.sessions_error is None,
            "sessions_error": reset.sessions_error,
            "sessions_found": reset.sessions_found,
            "sessions_listed_after": reset.sessions_listed_after,
        },
    )
    if reset.password is None:
        note = f"Credentials reset for team {team_number} by {actor} failed: {reset.error}"
    else:
        note = f"Credentials reset for team {team_number} by {actor}: {reset.sessions_outcome}"
    DiscordTask.enqueue(LogToChannel(message=note))
    return reset


@dataclass(frozen=True)
class BulkPasswordResetResult:
    password_list: list[tuple[int, str, str]]
    failed_resets: list[tuple[str, str]]
    sessions_failed: list[tuple[str, str]]
    sessions_uncounted: list[str]

    def summary(self) -> str:
        total = len(self.password_list) + len(self.failed_resets)
        text = f"Reset {len(self.password_list)}/{total} passwords"
        if self.failed_resets:
            text += "; failed: " + "; ".join(f"{username}: {why}" for username, why in self.failed_resets)
        if self.sessions_failed:
            text += f"; {sessions_failure_summary(self.sessions_failed)}"
        if self.sessions_uncounted:
            text += "; could not count the sessions of " + ", ".join(self.sessions_uncounted)
        return text


def sessions_failure_summary(sessions_failed: list[tuple[str, str]]) -> str:
    """'could not end sessions for team01, team02: <why>', one clause per distinct reason."""
    by_reason: dict[str, list[str]] = {}
    for username, reason in sessions_failed:
        by_reason.setdefault(reason, []).append(username)
    return "; ".join(f"could not end sessions for {', '.join(names)}: {why}" for why, names in by_reason.items())


def reset_teams_credentials(team_numbers: list[int], actor: str) -> BulkPasswordResetResult:
    """Reset passwords and end sessions for multiple teams; audit it and tell ops."""
    password_list: list[tuple[int, str, str]] = []
    failed_resets: list[tuple[str, str]] = []
    sessions_failed: list[tuple[str, str]] = []
    sessions_found = 0
    sessions_listed_after = 0
    uncounted: list[str] = []

    with AuthentikManager() as manager:
        resets = [(team_num, _reset_credentials(manager, team_num)) for team_num in team_numbers]
    for team_num, reset in resets:
        username = team_username(team_num)
        if reset.password is None:
            failed_resets.append((username, reset.error))
            continue
        password_list.append((team_num, username, reset.password))
        sessions_listed_after += reset.sessions_listed_after
        if reset.sessions_found is None:
            uncounted.append(username)
        else:
            sessions_found += reset.sessions_found
        if reset.sessions_error:
            sessions_failed.append((username, reset.sessions_error))
    result = BulkPasswordResetResult(
        password_list=password_list,
        failed_resets=failed_resets,
        sessions_failed=sessions_failed,
        sessions_uncounted=uncounted,
    )

    AuditLog.objects.create(
        action="blueteam_passwords_reset",
        admin_user=actor,
        target_entity="authentik_users",
        target_id=0,
        details={
            "team_numbers": team_numbers,
            "total_users": len(team_numbers),
            "success_count": len(password_list),
            "failed_count": len(failed_resets),
            "failed_resets": [f"{username}: {why}" for username, why in failed_resets],
            "sessions_failed": [f"{username}: {why}" for username, why in sessions_failed],
            "sessions_found": sessions_found,
            "sessions_uncounted": uncounted,
            "sessions_listed_after": sessions_listed_after,
        },
    )
    DiscordTask.enqueue(LogToChannel(message=f"Credentials reset by {actor}: {result.summary()}"))
    return result


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
