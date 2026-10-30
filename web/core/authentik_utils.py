import csv
import io
import logging
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import NamedTuple

from django.contrib.auth.models import User
from django.db import DatabaseError, transaction
from django.utils import timezone

from team.models import MAX_TEAMS, SchoolInfo, Team, team_username

from .auth_utils import lock_account, sign_out_everywhere
from .authentik_manager import SESSIONS_HIDDEN, AuthentikAPIError, AuthentikManager, AuthentikUser, reason_for
from .discord_tasks import LogToChannel, SetupTeamInfrastructure
from .models import AuditLog, CredentialsReset, DiscordTask
from .services.linking import CREDENTIALS_RESET, TEAM_RESET, unlink_teams
from .utils import shorten

logger = logging.getLogger(__name__)


def _team_user(manager: AuthentikManager, team_number: int) -> AuthentikUser:
    """The team's Authentik account; AuthentikAPIError saying why when there is none."""
    if team_number < 1 or team_number > MAX_TEAMS:
        raise AuthentikAPIError(f"Team number must be between 1 and {MAX_TEAMS}")
    username = team_username(team_number)
    user = manager.find_user(username)
    if user is None:
        raise AuthentikAPIError(f"User {username} not found")
    return user


def set_team_account_active(manager: AuthentikManager, team_number: int, is_active: bool) -> None:
    """Enable or disable the team's Authentik account; AuthentikAPIError when it can't."""
    manager.set_user_active(_team_user(manager, team_number), is_active)


# The portal refuses a sign-in from before the reset, but the team's other Authentik apps don't
STILL_OPEN = "an open Authentik session still reaches the team's other Authentik apps"


def _unlinked_text(count: int) -> str:
    return f"unlinked {count} Discord member(s), who link again with the new password"


@dataclass(frozen=True)
class SignedOut:
    unlinked: int


@dataclass(frozen=True)
class Unlinked:
    count: int


@dataclass(frozen=True)
class FollowupFailed:
    why: str


@dataclass(frozen=True)
class NotRun:
    pass


def _followup_details(followup: SignedOut | Unlinked | FollowupFailed | NotRun) -> dict[str, object]:
    match followup:
        case SignedOut(unlinked=count) | Unlinked(count=count):
            return {"discord_unlinked": count, "followup_error": None}
        case FollowupFailed(why=why):
            return {"discord_unlinked": 0, "followup_error": why}
        case NotRun():
            return {"discord_unlinked": 0, "followup_error": None}


@dataclass(frozen=True)
class Changed:
    """Authentik took the new password. sessions_found: counted before the change, None if unknown. store_error: why
    the copy packet emails send wasn't stored. record_error: why the reset time (core.oauth) wasn't recorded."""

    password: str
    sessions_error: str | None = None
    sessions_found: int | None = None
    sessions_listed_after: int = 0
    store_error: str | None = None
    record_error: str | None = None
    followup: SignedOut | FollowupFailed = SignedOut(0)

    @property
    def unlinked(self) -> int:
        return self.followup.unlinked if isinstance(self.followup, SignedOut) else 0

    @property
    def members_unlinked(self) -> bool:
        return isinstance(self.followup, SignedOut)

    @property
    def complete(self) -> bool:
        failed = self.sessions_error or self.store_error or self.record_error
        return not (failed or isinstance(self.followup, FollowupFailed))

    def problems(self) -> list[str]:
        problems = []
        if self.store_error:
            problems.append(f"could not store the new password for packet emails: {self.store_error}")
        if self.record_error:
            problems.append(
                f"could not record the reset, so a sign-in from before it can still come back in: {self.record_error}"
            )
        if isinstance(self.followup, FollowupFailed):
            problems.append(
                f"could not sign the team account out of the portal or end its Discord links: {self.followup.why}"
            )
        return problems

    def describe(self) -> str:
        if self.sessions_error:
            parts = [f"could not end the team's Authentik sessions ({self.sessions_error}), so {STILL_OPEN}"]
        elif self.sessions_found is None:
            parts = ["ended the team's Authentik sessions, but could not count them first"]
        else:
            parts = ["ended the team's Authentik sessions"]
        parts += self.problems()
        if isinstance(self.followup, SignedOut):
            parts.append("signed the team account out of the portal")
            if self.followup.unlinked:
                parts.append(_unlinked_text(self.followup.unlinked))
        return "; ".join(parts)

    def audit_details(self) -> dict[str, object]:
        return {
            "password_reset": True,
            "sessions_revoked": self.sessions_error is None,
            "sessions_error": self.sessions_error,
            "sessions_found": self.sessions_found,
            "sessions_listed_after": self.sessions_listed_after,
            "store_error": self.store_error,
            "record_error": self.record_error,
            **_followup_details(self.followup),
        }


@dataclass(frozen=True)
class Refused:
    """Authentik kept the old password, or the account couldn't be found."""

    error: str
    sessions_found: int | None = None
    followup: Unlinked | FollowupFailed | NotRun = NotRun()

    @property
    def unlinked(self) -> int:
        return self.followup.count if isinstance(self.followup, Unlinked) else 0

    @property
    def members_unlinked(self) -> bool:
        return isinstance(self.followup, Unlinked)

    def describe(self) -> str:
        parts = [f"the password reset failed: {self.error}"]
        match self.followup:
            case Unlinked(count=count):
                parts.append(f"unlinked {count} Discord member(s)")
            case FollowupFailed(why=why):
                parts.append(f"could not end the team's Discord links: {why}")
            case NotRun():
                pass
        return "; ".join(parts)

    def audit_details(self) -> dict[str, object]:
        return {
            "password_reset": False,
            "sessions_revoked": False,
            "sessions_error": None,
            "sessions_found": self.sessions_found,
            "sessions_listed_after": 0,
            "store_error": None,
            "record_error": None,
            **_followup_details(self.followup),
        }


type CredentialReset = Changed | Refused


def _count_sessions(manager: AuthentikManager, user: AuthentikUser) -> tuple[int | None, bool | None]:
    """(session count or None; whether the token sees no sessions at all, None when the count failed)."""
    try:
        session_ids = manager.user_sessions(user)
        if not session_ids and not manager.sees_any_sessions():
            return None, True
    except AuthentikAPIError as e:
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
    except AuthentikAPIError as e:
        logger.warning(f"Ending the sessions of {user['username']} failed: {e}")
        return found, reason_for(e)


def _record_reset(user: AuthentikUser, changed_at: datetime) -> None:
    # A sign-in from before the change must not come back in through an authorization code issued then (core.oauth)
    lock_account(user["uid"])
    reset, created = CredentialsReset.objects.get_or_create(authentik_id=user["uid"], defaults={"reset_at": changed_at})
    if not created and reset.reset_at < changed_at:
        reset.reset_at = changed_at
        reset.save(update_fields=["reset_at"])


def _end_portal_sessions(user: AuthentikUser) -> None:
    lock_account(user["uid"])
    for account in User.objects.filter(usergroups__authentik_id=user["uid"]):
        sign_out_everywhere(account)


def _store_password(team_number: int, password: str) -> None:
    # The copy packet emails send; a team with no school info has none
    SchoolInfo.objects.filter(team__team_number=team_number).update(password=password)


def set_team_password(team_number: int, actor: str) -> tuple[str | None, str]:
    """A new random password for the team account, leaving its sessions alone; audited."""
    with AuthentikManager() as manager:
        try:
            password, error = generate_blueteam_password(), ""
            manager.set_password(_team_user(manager, team_number), password)
        except AuthentikAPIError as e:
            logger.warning(f"Failed to set the password of team {team_number}: {e}")
            password, error = None, reason_for(e)
    if password is not None:
        _store_password(team_number, password)
    AuditLog.objects.create(
        action="team_password_set",
        admin_user=actor,
        target_entity="team",
        target_id=team_number,
        details={"password_set": password is not None, "error": error or None},
    )
    return password, error


class _NewCredentials(NamedTuple):
    reset: CredentialReset
    user: AuthentikUser | None = None
    changed_at: datetime | None = None


def _new_credentials(manager: AuthentikManager, team_number: int) -> _NewCredentials:
    """The new password, then the Authentik sessions ended: an open one still reaches the other Authentik apps."""
    try:
        user = _team_user(manager, team_number)
    except AuthentikAPIError as e:
        return _NewCredentials(Refused(reason_for(e)))
    # Before the password changes: Authentik may end sessions itself when it does, leaving nothing to count
    found, hidden = _count_sessions(manager, user)
    password = generate_blueteam_password()
    try:
        manager.set_password(user, password)
    except AuthentikAPIError as e:
        logger.warning(f"Failed to set the password of team {team_number}: {e}")
        return _NewCredentials(Refused(reason_for(e), sessions_found=found), user)
    # After: nobody can sign in with the new password before Authentik answers, but the old one works until then
    changed_at = timezone.now()
    listed_after, sessions_error = _end_sessions(manager, user, hidden)
    return _NewCredentials(Changed(password, sessions_error, found, listed_after), user, changed_at)


def _in_own_transaction(what: str, step: Callable[[], object]) -> str | None:
    """Run a database step in a transaction of its own: None, or why it was undone."""
    try:
        with transaction.atomic():
            step()
    except DatabaseError as e:
        logger.error(f"{what} failed: {e}")
        return str(e) or type(e).__name__
    return None


def _reset_credentials(
    manager: AuthentikManager, team_number: int, actor: str, reason: str, *, unlink_on_failure: bool
) -> CredentialReset:
    """Each database step after the password change runs in a transaction of its own, the stored copy first."""
    reset, user, changed_at = _new_credentials(manager, team_number)
    if isinstance(reset, Refused) and not unlink_on_failure:
        return reset
    if isinstance(reset, Changed) and user is not None and changed_at is not None:
        password, changed_user, changed = reset.password, user, changed_at
        store_error = _in_own_transaction(
            f"Storing the new password of team {team_number}", lambda: _store_password(team_number, password)
        )
        # Apart from the sign-out and unlink, so their failure can't undo it
        record_error = _in_own_transaction(
            f"Recording the credentials reset of team {team_number}", lambda: _record_reset(changed_user, changed)
        )
        reset = replace(reset, store_error=store_error, record_error=record_error)
    unlinked: list[int] = []

    def follow_up() -> None:
        if isinstance(reset, Changed) and user is not None:
            _end_portal_sessions(user)
        unlinked.append(len(unlink_teams(Team.objects.filter(team_number=team_number), actor, reason)))

    why = _in_own_transaction(f"Signing team {team_number} out of the portal and unlinking it", follow_up)
    match reset:
        case Changed():
            return replace(reset, followup=FollowupFailed(why) if why else SignedOut(unlinked[0]))
        case Refused():
            return replace(reset, followup=FollowupFailed(why) if why else Unlinked(unlinked[0]))


def reset_team_credentials(team_number: int, actor: str) -> CredentialReset:
    """A new password, then the account's sessions and its members' Discord links ended once it took. The audit row
    and ops note can't fail the reset: the new password must reach the caller."""
    with AuthentikManager() as manager:
        reset = _reset_credentials(manager, team_number, actor, CREDENTIALS_RESET, unlink_on_failure=False)

    def record() -> None:
        AuditLog.objects.create(
            action="team_password_reset",
            admin_user=actor,
            target_entity="team",
            target_id=team_number,
            details=reset.audit_details(),
        )
        message = f"Credentials reset for team {team_number} by {actor}: {reset.describe()}"
        DiscordTask.enqueue(LogToChannel(message=message))

    _in_own_transaction(f"Recording the credentials reset of team {team_number}", record)
    return reset


class TeamReset(NamedTuple):
    credentials: CredentialReset
    recreation_queued: bool


def reset_team(team_number: int, actor: str, *, recreate_channels: bool) -> TeamReset:
    """Reset Team: unlinked even when the password change fails, channels recreated when asked (queued), audited and
    told to ops once; the record can't fail the reset."""
    team = Team.objects.get(team_number=team_number)
    with AuthentikManager() as manager:
        reset = _reset_credentials(manager, team_number, actor, TEAM_RESET, unlink_on_failure=True)

    def record() -> None:
        if recreate_channels:
            DiscordTask.enqueue(SetupTeamInfrastructure(team_number=team_number, recreate=True))
        AuditLog.objects.create(
            action="team_reset",
            admin_user=actor,
            target_entity="team",
            target_id=team_number,
            details={
                "team_name": team.team_name,
                "unlinked_members": reset.unlinked,
                "recreated_channels": recreate_channels,
                **reset.audit_details(),
            },
        )
        channels = "the role and channels are being recreated" if recreate_channels else "channels kept"
        message = f"Team reset of {team.team_name} by {actor}: {reset.describe()}; {channels}"
        DiscordTask.enqueue(LogToChannel(message=message))

    recorded = _in_own_transaction(f"Recording the reset of team {team_number}", record) is None
    return TeamReset(reset, recreate_channels and recorded)


@dataclass(frozen=True)
class BulkPasswordResetResult:
    """Each team's CredentialReset, in the order asked; the text and the audit row are read from them."""

    resets: list[tuple[int, CredentialReset]]

    @property
    def password_list(self) -> list[tuple[int, str, str]]:
        return [(n, team_username(n), r.password) for n, r in self.resets if isinstance(r, Changed)]

    @property
    def failed_resets(self) -> list[tuple[str, str]]:
        return [(team_username(n), r.error) for n, r in self.resets if isinstance(r, Refused)]

    @property
    def complete(self) -> bool:
        """Every team's password changed with nothing left undone."""
        return all(isinstance(r, Changed) and r.complete for _, r in self.resets)

    @property
    def headline(self) -> str:
        return f"Reset {len(self.password_list)}/{len(self.resets)} passwords"

    def details(self) -> list[str]:
        """One clause per failure (the sessions grouped by reason), and how many Discord members were unlinked."""
        changed = [(team_username(n), r) for n, r in self.resets if isinstance(r, Changed)]
        lines = [f"failed {name}: {why}" for name, why in self.failed_resets]
        if sessions_failed := [(name, r.sessions_error) for name, r in changed if r.sessions_error]:
            lines.append(sessions_failure_summary(sessions_failed))
        if uncounted := [name for name, r in changed if r.sessions_found is None]:
            lines.append("could not count the sessions of " + ", ".join(uncounted))
        lines += [f"{name}: {problem}" for name, r in changed for problem in r.problems()]
        if unlinked := sum(r.unlinked for _, r in changed):
            lines.append(_unlinked_text(unlinked))
        return lines

    def summary(self) -> str:
        return "; ".join([self.headline, *self.details()])

    def audit_details(self) -> dict[str, object]:
        return {
            "team_numbers": [n for n, _ in self.resets],
            "total_users": len(self.resets),
            "success_count": len(self.password_list),
            "failed_count": len(self.failed_resets),
            "teams": {team_username(n): r.audit_details() for n, r in self.resets},
        }


def sessions_failure_summary(sessions_failed: list[tuple[str, str]]) -> str:
    """'could not end sessions for team01, team02 (<why>), so ...', one clause per distinct reason."""
    by_reason: dict[str, list[str]] = {}
    for username, reason in sessions_failed:
        by_reason.setdefault(reason, []).append(username)
    return "; ".join(
        f"could not end sessions for {', '.join(names)} ({why}), so {STILL_OPEN}" for why, names in by_reason.items()
    )


def reset_teams_credentials(team_numbers: list[int], actor: str) -> BulkPasswordResetResult:
    """_reset_credentials for several teams, with one audit row and one ops note, which can't fail it: every new
    password must reach the caller."""
    with AuthentikManager() as manager:
        result = BulkPasswordResetResult(
            [
                (n, _reset_credentials(manager, n, actor, CREDENTIALS_RESET, unlink_on_failure=False))
                for n in team_numbers
            ]
        )

    def record() -> None:
        AuditLog.objects.create(
            action="blueteam_passwords_reset",
            admin_user=actor,
            target_entity="authentik_users",
            target_id=0,
            details=result.audit_details(),
        )
        DiscordTask.enqueue(LogToChannel(message=f"Credentials reset by {actor}: {result.summary()}"))

    _in_own_transaction("Recording the bulk credentials reset", record)
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
