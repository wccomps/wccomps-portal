import re
from collections.abc import Callable
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import MaxLengthValidator
from django.db import models, transaction
from django.db.models import Sum
from django.db.models.functions import Length
from django.utils import timezone

from core.models import Approvable
from core.stored_files import StoredFile
from core.utils import advisory_xact_lock


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
    # The topic in the team's Zulip stream that the ticket-created notice opened; every later notice goes to it.
    # Blank until then
    zulip_topic = models.CharField(max_length=60, blank=True, db_default="", default="")

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
    # Files live in Postgres, so these bound what one team can fill it with, whichever way the files come in
    MAX_PER_TICKET = 20
    MAX_TEAM_BYTES = 200 * 1024 * 1024
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

    def store(self, before_save: Callable[[], str] = lambda: "") -> str:
        """Save the file unless its ticket is resolved or cancelled, already holds MAX_PER_TICKET files, or the team's
        tickets would hold more than MAX_TEAM_BYTES with it, or then before_save refuses it; why not, or "" once saved.
        Checked, before_save run and saved under an advisory lock on the team, so uploads at once can't pass a cap
        together."""
        team_id = self.ticket.team_id
        with transaction.atomic():
            advisory_xact_lock(f"ticket_attachments:team:{team_id}")
            status = Ticket.objects.values_list("status", flat=True).get(pk=self.ticket_id)
            if status in (Ticket.STATUS_RESOLVED, Ticket.STATUS_CANCELLED):
                return f"Ticket is {status}; files can't be added to it"
            if TicketAttachment.objects.filter(ticket_id=self.ticket_id).count() >= self.MAX_PER_TICKET:
                return f"Ticket already has the maximum of {self.MAX_PER_TICKET} attachments"
            stored = TicketAttachment.objects.filter(ticket__team_id=team_id).aggregate(total=Sum(Length("file_data")))
            if (stored["total"] or 0) + len(self.file_data) > self.MAX_TEAM_BYTES:
                return f"Your team's attachments would pass their {self.MAX_TEAM_BYTES // (1024 * 1024)}MB total limit"
            if refused := before_save():
                return refused
            self.save()
        return ""


class TicketComment(models.Model):
    """Comment on ticket (bidirectional with the chat backends)."""

    # Two Discord messages' worth; the web form and every chat path refuse a longer one
    MAX_LENGTH = 4000

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_comments",
        help_text="User who authored this comment",
    )
    comment_text = models.TextField(validators=[MaxLengthValidator(MAX_LENGTH)])
    posted_at = models.DateTimeField(auto_now_add=True)
    discord_message_id = models.BigIntegerField(null=True, blank=True)
    zulip_message_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["posted_at"]

    @property
    def author_name(self) -> str:
        """Who wrote it, as chat shows it: the portal user's username, else "Unknown"."""
        return self.author.username if self.author else "Unknown"

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
    """Rate limiting for ticket comments and uploads (5/min per ticket, 10/min per actor), on every surface.

    actor_key names who posted: "user:<User id>" (user_key), so a team's shared account has one budget across the web,
    Discord and Zulip.
    """

    TICKET_LIMIT = 5
    ACTOR_LIMIT = 10
    WINDOW = timedelta(minutes=1)

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, null=True, blank=True)
    # db_default: the previous release's pods insert rows without it while a deploy rolls out
    actor_key = models.CharField(max_length=64, db_default="")
    posted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["ticket", "-posted_at"]),
            models.Index(fields=["actor_key", "-posted_at"]),
        ]

    def __str__(self) -> str:
        return f"Comment by {self.actor_key} at {self.posted_at}"

    @staticmethod
    def user_key(user: User) -> str:
        return f"user:{user.pk}"

    @classmethod
    def take(cls, ticket: Ticket, actor_key: str) -> str:
        """Count a post against the limits; why they refuse it (nothing counted), or "" once counted. The check and
        the count run under advisory locks on the ticket, then the actor, so posts at once can't all pass; inside a
        caller's transaction the locks are held until it ends."""
        with transaction.atomic():
            advisory_xact_lock(f"comment_rate:ticket:{ticket.pk}")
            advisory_xact_lock(f"comment_rate:actor:{actor_key}")
            since = timezone.now() - cls.WINDOW
            cls.objects.filter(ticket_id=ticket.pk, actor_key=actor_key, posted_at__lt=since).delete()
            ticket_posts = cls.objects.filter(ticket_id=ticket.pk, posted_at__gte=since).count()
            if ticket_posts >= cls.TICKET_LIMIT:
                return f"Ticket rate limit exceeded ({ticket_posts}/{cls.TICKET_LIMIT} comments in last minute)"
            actor_posts = cls.objects.filter(actor_key=actor_key, posted_at__gte=since).count()
            if actor_posts >= cls.ACTOR_LIMIT:
                return f"User rate limit exceeded ({actor_posts}/{cls.ACTOR_LIMIT} comments in last minute)"
            cls.objects.create(ticket=ticket, actor_key=actor_key)
        return ""
