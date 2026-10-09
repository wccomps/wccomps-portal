import re
from datetime import timedelta

from django.db import models
from django.utils import timezone


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

    class Meta:
        ordering = ["sort_order", "display_name"]
        verbose_name_plural = "ticket categories"

    def __str__(self) -> str:
        return self.display_name


class Ticket(models.Model):
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
    custom_fields = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    tags = models.JSONField(default=list, blank=True)  # e.g., ['operations-issue', 'escalated']

    assigned_at = models.DateTimeField(null=True, blank=True)

    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    duration_notes = models.TextField(blank=True)
    points_charged = models.IntegerField(default=0)

    is_approved = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_tickets",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_notes = models.TextField(blank=True)

    discord_thread_id = models.BigIntegerField(unique=True, null=True, blank=True)
    discord_channel_id = models.BigIntegerField(null=True, blank=True)
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
        return f"{self.ticket_number} - Team {self.team.team_number}"


class TicketAttachment(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    file_data = models.BinaryField()
    filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    uploaded_by = models.CharField(max_length=100)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self) -> str:
        return f"{self.filename} ({self.ticket.ticket_number})"


class TicketComment(models.Model):
    """Comment on ticket (bidirectional with Discord)."""

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

    class Meta:
        ordering = ["posted_at"]

    def __str__(self) -> str:
        author_name = str(self.author) if self.author else "Unknown"
        return f"Comment by {author_name} on {self.ticket.ticket_number}"


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
    """Rate limiting for ticket comments (5/min per ticket, 10/min per user)."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, null=True, blank=True)
    discord_id = models.BigIntegerField()
    posted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["ticket", "-posted_at"]),
            models.Index(fields=["discord_id", "-posted_at"]),
        ]

    def __str__(self) -> str:
        return f"Comment by {self.discord_id} at {self.posted_at}"

    @classmethod
    def check_rate_limit(cls, ticket_id: int, discord_id: int) -> tuple[bool, str]:
        """Return (is_allowed, reason_if_blocked)."""
        one_minute_ago = timezone.now() - timedelta(minutes=1)

        ticket_comments = cls.objects.filter(ticket_id=ticket_id, posted_at__gte=one_minute_ago).count()

        if ticket_comments >= 5:
            return (
                False,
                f"Ticket rate limit exceeded ({ticket_comments}/5 comments in last minute)",
            )

        user_comments = cls.objects.filter(discord_id=discord_id, posted_at__gte=one_minute_ago).count()

        if user_comments >= 10:
            return (
                False,
                f"User rate limit exceeded ({user_comments}/10 comments in last minute)",
            )

        return True, ""
