import re
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import Approvable
from core.stored_files import StoredFile


def parse_playbook_steps(text: str) -> list[str]:
    """Parse multiline playbook text into individual steps.

    Handles plain lines, bullet points (- , * , • ), and numbered lists (1. , 1) ).
    """
    if not text:
        return []
    steps: list[str] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = re.sub(r"^(\d+[\.\)]|\*|-|•|\[[\sxX]\])\s*", "", line).strip()
        if cleaned:
            steps.append(cleaned)
    return steps


BOX_RESET_CATEGORY_ID: int = settings.BOX_RESET_CATEGORY_ID


class TicketCategory(models.Model):
    display_name = models.CharField(max_length=100)
    points = models.IntegerField(default=0)
    required_fields = models.JSONField(default=list, blank=True)
    optional_fields = models.JSONField(default=list, blank=True)
    variable_points = models.BooleanField(default=False)
    variable_cost_note = models.TextField(blank=True)
    min_points = models.IntegerField(default=0)
    max_points = models.IntegerField(default=0)
    user_creatable = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    playbook = models.TextField(blank=True, default="", help_text="Staff response steps/playbook")

    @property
    def steps(self) -> list[str]:
        return parse_playbook_steps(self.playbook)

    @property
    def points_label(self) -> str:
        return "Variable" if self.variable_points else f"{self.points}pt"

    @property
    def points_display(self) -> str:
        return "Variable" if self.variable_points else f"{self.points} points"

    class Meta:
        ordering = ["sort_order", "display_name"]
        verbose_name_plural = "ticket categories"

    def __str__(self) -> str:
        return self.display_name


class Ticket(Approvable):
    """Support ticket from team."""

    STATUS_OPEN = "open"
    STATUS_CLAIMED = "claimed"
    STATUS_RESOLVED = "resolved"
    STATUS_CANCELLED = "cancelled"

    assigned_to = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
        help_text="User assigned to this ticket",
    )
    resolved_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_tickets",
        help_text="User who resolved this ticket",
    )

    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_CLAIMED, "Claimed"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    ticket_number = models.CharField(max_length=20, unique=True)
    team = models.ForeignKey("team.Team", on_delete=models.CASCADE, related_name="tickets")

    category = models.ForeignKey(
        "ticketing.TicketCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    title = models.TextField()
    description = models.TextField(blank=True)

    hostname = models.CharField(max_length=255, blank=True)
    ip_address = models.CharField(max_length=45, blank=True, null=True)
    service_name = models.CharField(max_length=100, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)

    assigned_at = models.DateTimeField(null=True, blank=True)

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    points_charged = models.IntegerField(default=0)

    discord_thread_id = models.BigIntegerField(unique=True, null=True, blank=True)
    discord_ticket_message_id = models.BigIntegerField(
        null=True, blank=True, help_text="The bot's ticket message with action buttons in the thread"
    )
    thread_archive_scheduled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["team", "status"]),
            models.Index(fields=["category", "status"]),
            models.Index(fields=["assigned_to"]),
            models.Index(fields=["discord_thread_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(ticket_number=""),
                name="ticket_number_not_empty",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ticket_number} (Team {self.team.team_number})"


class TicketAttachment(StoredFile):
    MAX_SIZE = 10 * 1024 * 1024  # 10MB, from the web or a ticket's chat topic
    DEFAULT_NAME = "attachment"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    uploaded_by = models.CharField(max_length=100)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # The Zulip message it came with, so a redelivered message stores it once
    zulip_message_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self) -> str:
        return f"{self.filename} ({self.ticket.ticket_number})"


class TicketComment(models.Model):
    """Comment on ticket (bidirectional with the chat backends)."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_comments",
        help_text="User who authored this comment",
    )
    comment_text = models.TextField()
    posted_at = models.DateTimeField(auto_now_add=True)
    discord_message_id = models.BigIntegerField(null=True, blank=True)
    zulip_message_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    external_author = models.CharField(
        max_length=255, blank=True, help_text="The chat display name of an author with no portal account"
    )

    class Meta:
        ordering = ["posted_at"]

    @property
    def author_name(self) -> str:
        """Who wrote it: the portal user's username, else the name chat showed for them, else "Unknown"."""
        return (self.author.username if self.author else self.external_author) or "Unknown"

    def __str__(self) -> str:
        return f"Comment by {self.author_name} on {self.ticket.ticket_number}"


class TicketHistory(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="history")
    action = models.CharField(max_length=50)
    actor = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_history_entries",
        help_text="User who performed this action",
    )
    details = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self) -> str:
        return f"{self.action} on {self.ticket.ticket_number}"


class CommentRateLimit(models.Model):
    """Rate limiting for ticket comments (5/min per ticket, 10/min per actor).

    actor_key names who posted: "web:<User id>", "discord:<Discord user id>" or "zulip:<Zulip user id>".
    """

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, null=True, blank=True)
    actor_key = models.CharField(max_length=64)
    posted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["ticket", "-posted_at"]),
            models.Index(fields=["actor_key", "-posted_at"]),
        ]

    def __str__(self) -> str:
        return f"Comment by {self.actor_key} at {self.posted_at}"

    @classmethod
    def check_rate_limit(cls, ticket_id: int, actor_key: str) -> tuple[bool, str]:
        """Return (is_allowed, reason_if_blocked)."""
        one_minute_ago = timezone.now() - timedelta(minutes=1)
        cls.objects.filter(posted_at__lt=one_minute_ago).delete()

        ticket_comments = cls.objects.filter(ticket_id=ticket_id, posted_at__gte=one_minute_ago).count()

        if ticket_comments >= 5:
            return (
                False,
                f"Ticket rate limit exceeded ({ticket_comments}/5 comments in last minute)",
            )

        user_comments = cls.objects.filter(actor_key=actor_key, posted_at__gte=one_minute_ago).count()

        if user_comments >= 10:
            return (
                False,
                f"User rate limit exceeded ({user_comments}/10 comments in last minute)",
            )

        return True, ""
