from dataclasses import asdict, fields
from datetime import datetime

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from core import discord_tasks


class UserGroups(models.Model):
    """
    Stores Authentik groups for a user. Refreshed on every login and by the bot every 5 minutes.

    This is the single source of truth for user permissions.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    authentik_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Authentik user UUID (sub claim)",
    )
    groups = models.JSONField(
        default=list,
        help_text="List of Authentik group names",
    )

    class Meta:
        verbose_name = "User Groups"
        verbose_name_plural = "User Groups"

    def __str__(self) -> str:
        return f"{self.user.username} ({len(self.groups)} groups)"


class AuditLog(models.Model):
    action = models.CharField(max_length=50)
    admin_user = models.CharField(max_length=255)
    target_entity = models.CharField(max_length=50)
    target_id = models.BigIntegerField()
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} by {self.admin_user}"


class DiscordTask(models.Model):
    """Work queued for the bot (bot/discord_queue.py); the task types are core/discord_tasks.py's payloads."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("processing", "Processing"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    task_type = models.CharField(
        max_length=50, choices=[(t, cls.label) for t, cls in discord_tasks.PAYLOAD_TYPES.items()]
    )
    ticket = models.ForeignKey("ticketing.Ticket", null=True, blank=True, on_delete=models.CASCADE)
    payload = models.JSONField(default=dict)
    result = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=5)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["status", "next_retry_at"]),
            models.Index(fields=["task_type", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.task_type} ({self.status})"

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        if self.task_type not in discord_tasks.PAYLOAD_TYPES:
            return  # task_type's choices report it
        try:
            self.typed_payload()
        except TypeError as e:
            raise ValidationError(f"Payload for {self.task_type} is invalid: {e}") from e

    def typed_payload(self) -> discord_tasks.TaskPayload:
        """Raises KeyError for an unknown task type, TypeError for a missing field.

        Keys the type doesn't declare are dropped: rows outlive the code that wrote them. A missing
        ticket_id comes from the ticket FK, where the previous release's web put it.
        """
        cls = discord_tasks.PAYLOAD_TYPES[self.task_type]
        values = {"ticket_id": self.ticket_id, **self.payload} if self.ticket_id is not None else self.payload
        return cls(**{f.name: values[f.name] for f in fields(cls) if f.name in values})

    @classmethod
    def enqueue(cls, payload: discord_tasks.TaskPayload) -> DiscordTask:
        """A payload's ticket_id also sets the ticket FK, so deleting a ticket drops its pending tasks."""
        return cls.objects.create(
            task_type=payload.task_type,
            ticket_id=getattr(payload, "ticket_id", None),
            payload=asdict(payload),
            status="pending",
        )


class BotState(models.Model):
    """Bot state storage (dashboard message IDs, etc)."""

    key = models.CharField(max_length=100, unique=True)
    value = models.CharField(max_length=255)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.key}: {self.value}"


class QueuedAnnouncement(models.Model):
    """Announcements queued for teams that don't have channels yet."""

    team = models.ForeignKey("team.Team", on_delete=models.CASCADE, related_name="queued_announcements")
    message = models.TextField()
    sender_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        status = "delivered" if self.delivered_at else "pending"
        return f"Announcement for Team {self.team.team_number} ({status})"


class CompetitionConfig(models.Model):
    max_team_members = models.IntegerField(default=10, help_text="Maximum members per team")

    competition_start_time = models.DateTimeField(
        null=True, blank=True, help_text="When applications should be enabled"
    )
    competition_end_time = models.DateTimeField(null=True, blank=True, help_text="When applications should be disabled")
    applications_enabled = models.BooleanField(default=False, help_text="Whether applications are currently enabled")

    controlled_applications = models.JSONField(
        default=list,
        help_text="List of Authentik application slugs to enable/disable (e.g., ['scoring', 'quotient2', 'semaphore'])",
    )
    start_message = models.TextField(
        blank=True,
        help_text="Posted to every active team's chat channel when the competition starts, e.g. default VM credentials",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    status_channel_id = models.BigIntegerField(
        null=True, blank=True, help_text="Discord channel ID for competition status display"
    )
    status_message_id = models.BigIntegerField(
        null=True, blank=True, help_text="Discord message ID for status embed (updated in place)"
    )

    class Meta:
        verbose_name = "Competition Configuration"
        verbose_name_plural = "Competition Configuration"

    def __str__(self) -> str:
        if self.competition_start_time:
            return f"Competition starts at {self.competition_start_time} (enabled={self.applications_enabled})"
        return "Competition not scheduled"

    def schedule_error(self, start: datetime | None = None, end: datetime | None = None) -> str | None:
        """Why the schedule would be unusable after setting start and/or end (keeping the stored other), or None.

        With the end at or before the start, should_enable_applications is never true: nothing starts.
        """
        start = start or self.competition_start_time
        end = end or self.competition_end_time
        if start and end and end <= start:
            return "The end time must be after the start time"
        return None

    def should_enable_applications(self) -> bool:
        if not self.competition_start_time:
            return False
        now = timezone.now()
        after_start = now >= self.competition_start_time
        before_end = self.competition_end_time is None or now < self.competition_end_time
        return after_start and before_end and not self.applications_enabled

    def should_disable_applications(self) -> bool:
        if not self.competition_end_time:
            return False
        return timezone.now() >= self.competition_end_time and self.applications_enabled

    @classmethod
    def get_config(cls) -> CompetitionConfig:
        """Get or create the singleton config instance (pk=1). Never create additional rows."""
        config, _ = cls.objects.get_or_create(pk=1)
        return config
