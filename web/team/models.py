import logging
from collections.abc import Iterable

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import models
from django.db.models.base import ModelBase
from django.utils import timezone
from django.utils.functional import cached_property

logger = logging.getLogger(__name__)

MAX_TEAMS = 50


def team_username(team_number: int) -> str:
    """Username of a team's shared Authentik account."""
    return f"team{team_number:02d}"


def active_team_numbers() -> list[int]:
    """Numbers of the teams competing in the current event, in order."""
    return list(Team.objects.filter(is_active=True).order_by("team_number").values_list("team_number", flat=True))


def default_team_name(team_number: int) -> str:
    """A team's name until someone gives it one; the school CSV import resets every team to it."""
    return f"BlueTeam{team_number:02d}"


class Team(models.Model):
    """Competition team (1-50)."""

    team_number = models.IntegerField(unique=True)
    team_name = models.CharField(max_length=100)
    authentik_group = models.CharField(max_length=255, blank=True)

    discord_role_id = models.BigIntegerField(null=True, blank=True)
    discord_category_id = models.BigIntegerField(null=True, blank=True)

    ticket_counter = models.IntegerField(default=0)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["team_number"]

    def __str__(self) -> str:
        return f"Team {self.team_number}"

    def clean(self) -> None:
        super().clean()

        if not self.authentik_group and self.team_number:
            self.authentik_group = f"WCComps_BlueTeam{self.team_number:02d}"

        if self.team_number is not None and (self.team_number < 1 or self.team_number > MAX_TEAMS):
            msg = f"Team number must be between 1 and {MAX_TEAMS}, got {self.team_number}"
            raise ValidationError({"team_number": msg})

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        # Skip validation when using update_fields (e.g., with F() expressions)
        if not update_fields:
            self.full_clean()
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def get_member_count(self) -> int:
        """Get count of active members."""
        return self.members.filter(is_active=True).count()

    @cached_property
    def max_members(self) -> int:
        """The competition-wide member limit."""
        from core.models import CompetitionConfig

        return CompetitionConfig.get_config().max_team_members

    def is_full(self) -> bool:
        return self.get_member_count() >= self.max_members

    @property
    def school_emails(self) -> list[str]:
        """Contact emails for this team from SchoolInfo, or empty list if none."""
        try:
            return self.school_info.emails
        except ObjectDoesNotExist:
            return []


class DiscordLink(models.Model):
    """Link between Discord user and Authentik account (optionally part of a team)."""

    discord_id = models.BigIntegerField()
    discord_username = models.CharField(max_length=255)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="discord_links",
    )
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members", null=True, blank=True)
    is_active = models.BooleanField(default=True)
    linked_at = models.DateTimeField(auto_now_add=True)
    unlinked_at = models.DateTimeField(null=True, blank=True)

    is_student_helper = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Whether this person is currently a student helper",
    )
    helper_role_name = models.CharField(
        max_length=100,
        blank=True,
        help_text='Discord role name (e.g., "UCI Invitationals 2026")',
    )
    helper_role_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Discord role ID (snowflake)",
    )
    helper_activated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When helper access was granted",
    )
    helper_deactivated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When helper access was revoked",
    )
    helper_removal_reason = models.TextField(
        blank=True,
        help_text="Reason for helper access removal",
    )

    class Meta:
        indexes = [
            models.Index(fields=["discord_id", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["discord_id"],
                condition=models.Q(is_active=True),
                name="team_unique_active_discord_link",
            ),
        ]

    def __str__(self) -> str:
        if self.team:
            return f"{self.discord_username} → Team {self.team.team_number}"
        return f"{self.discord_username} → {self.user.username}"

    def set_helper(self, role_name: str, role_id: int | None = None) -> None:
        self.is_student_helper = True
        self.helper_role_name = role_name
        if role_id is not None:
            self.helper_role_id = role_id
        self.helper_activated_at = timezone.now()
        self.helper_deactivated_at = None
        self.helper_removal_reason = ""
        self.save()

    def remove_helper(self, reason: str = "") -> None:
        if self.is_student_helper:
            self.is_student_helper = False
            self.helper_deactivated_at = timezone.now()
            self.helper_removal_reason = reason
            self.save()

    @classmethod
    def deactivate_previous_links(cls, discord_id: int, exclude_pk: int | None = None) -> int:
        """Deactivate this Discord user's active links (returns the count); call before creating a new link."""
        qs = cls.objects.filter(discord_id=discord_id, is_active=True)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        return qs.update(is_active=False, unlinked_at=timezone.now())

    def save(
        self,
        *,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class LinkToken(models.Model):
    """Temporary token for linking flow."""

    token = models.CharField(max_length=64, unique=True)
    discord_id = models.BigIntegerField()
    discord_username = models.CharField(max_length=255)
    used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["token", "used", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"Token for {self.discord_username}"

    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at


class LinkAttempt(models.Model):
    """Audit log for link attempts."""

    discord_id = models.BigIntegerField()
    discord_username = models.CharField(max_length=255)
    authentik_username = models.CharField(max_length=255)
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL)
    success = models.BooleanField()
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self) -> str:
        status = "Success" if self.success else "Failed"
        return f"{self.discord_username} → {self.authentik_username} ({status})"


class LinkRateLimit(models.Model):
    """Rate limiting for link attempts (5 per hour per user)."""

    discord_id = models.BigIntegerField()
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["discord_id", "-attempted_at"]),
        ]

    def __str__(self) -> str:
        return f"Link attempt by {self.discord_id} at {self.attempted_at}"

    @classmethod
    def check_rate_limit(cls, discord_id: int) -> tuple[bool, int]:
        """Check the 5-attempts-per-hour limit, returning (is_allowed, attempts_in_last_hour)."""
        from datetime import timedelta

        one_hour_ago = timezone.now() - timedelta(hours=1)

        recent_attempts = cls.objects.filter(discord_id=discord_id, attempted_at__gte=one_hour_ago).count()

        return recent_attempts < 5, recent_attempts


class SchoolInfo(models.Model):
    """School information for teams (GoldTeam only)."""

    team = models.OneToOneField(Team, on_delete=models.CASCADE, related_name="school_info")
    school_name = models.CharField(max_length=255)
    contact_email = models.EmailField()
    secondary_email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    # The team account's password for this competition, sent in packets. Set by every password reset and by the
    # first packet send; the school list import replaces every row, so the next competition gets a new one.
    password = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "The team account's current Authentik password, sent in packets. "
            "Change it with Reset Password on the team's page."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "School Information"
        verbose_name_plural = "School Information"
        ordering = ["team__team_number"]

    def __str__(self) -> str:
        return f"{self.school_name} (Team {self.team.team_number})"

    @property
    def emails(self) -> list[str]:
        """All non-empty contact emails for the school."""
        return [e for e in (self.contact_email, self.secondary_email) if e]
