from typing import TYPE_CHECKING

from django.db import models
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from registration.models import Event

if TYPE_CHECKING:
    from team.models import Team


# A packet whose distribution has started went out to teams
SENT_STATUSES = ("distributing", "completed")


class PacketQuerySet(models.QuerySet["Packet"]):
    def sent(self) -> PacketQuerySet:
        """Packets whose distribution has started."""
        return self.filter(status__in=SENT_STATUSES)

    def downloadable(self) -> PacketQuerySet:
        """Sent packets that teams may download from the portal."""
        return self.sent().filter(web_access_enabled=True)


def packets_sent(event: Event | None) -> bool:
    """Whether any packet went out for the event; sent packets reached its teams under their schools."""
    return event is not None and Packet.objects.sent().filter(event=event).exists()


class Packet(models.Model):
    """Pre-competition information packet to be distributed to all teams."""

    objects = PacketQuerySet.as_manager()

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("distributing", "Distributing"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]

    event = models.ForeignKey(
        "registration.Event",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="packets",
        help_text="Set to the open event when the packet is sent",
    )
    title = models.CharField(max_length=255, help_text="Packet title/description")
    file_data = models.BinaryField(help_text="Packet file stored as binary data")
    filename = models.CharField(max_length=255, help_text="Original filename")
    mime_type = models.CharField(max_length=100, help_text="MIME type of the file")
    file_size = models.IntegerField(help_text="File size in bytes")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft", db_index=True)

    actual_distribution_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When distribution actually started",
    )

    send_via_email = models.BooleanField(default=True, help_text="Send packet via email to team contacts")
    web_access_enabled = models.BooleanField(default=True, help_text="Allow teams to download from web interface")

    # Per-team data included in emails (keyed by team number as string)
    team_extras = models.JSONField(default=dict, blank=True, help_text="Per-team data for emails, keyed by team number")

    uploaded_by = models.CharField(max_length=255, help_text="Username who uploaded")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True, help_text="Internal notes about this packet")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Packet"
        verbose_name_plural = "Packets"

    def __str__(self) -> str:
        return f"{self.title} ({self.status})"

    def get_distribution_stats(self) -> dict[str, int]:
        distributions = self.distributions.all()
        return {
            "total": distributions.count(),
            "pending": distributions.filter(email_status="pending").count(),
            "sent": distributions.filter(email_status="sent").count(),
            "failed": distributions.filter(email_status="failed").count(),
            "downloaded": distributions.filter(downloaded_at__isnull=False).count(),
        }

    def is_ready_for_distribution(self) -> bool:
        return self.status == "draft"

    @property
    def is_sent(self) -> bool:
        """Whether its distribution has started."""
        return self.status in SENT_STATUSES

    def mark_as_distributing(self) -> None:
        self.status = "distributing"
        self.actual_distribution_time = timezone.now()
        self.save(update_fields=["status", "actual_distribution_time", "updated_at"])

    def mark_as_completed(self) -> None:
        self.status = "completed"
        self.save(update_fields=["status", "updated_at"])

    def complete_if_done(self) -> None:
        """Mark a distributing packet completed once every team's email has gone out.

        Each send calls this, so the last one completes the packet whether or not anyone is watching.
        """
        if (
            self.send_via_email
            and self.distributions.exclude(email_status__in=PacketDistribution.DONE_STATUSES).exists()
        ):
            return
        Packet.objects.filter(pk=self.pk, status="distributing").update(status="completed", updated_at=timezone.now())


class PacketDistributionQuerySet(models.QuerySet["PacketDistribution"]):
    def visible_to(self, team: Team | None) -> PacketDistributionQuerySet:
        """The team's web-enabled packets sent for the open event (team numbers are reshuffled between events)."""
        event = Event.objects.open()
        if event is None:
            return self.none()
        return self.filter(
            team=team, web_access_enabled=True, packet__in=Packet.objects.downloadable().filter(event=event)
        )


class PacketDistribution(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("sent", "Sent"),
        ("failed", "Failed"),
    ]
    DONE_STATUSES = ("sent",)

    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="distributions")
    team = models.ForeignKey("team.Team", on_delete=models.CASCADE)

    objects = PacketDistributionQuerySet.as_manager()

    email_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    email_sent_to = models.EmailField(blank=True, help_text="Email address where packet was sent")
    email_sent_at = models.DateTimeField(null=True, blank=True)
    email_error_message = models.TextField(blank=True)

    web_access_enabled = models.BooleanField(default=True)
    downloaded_at = models.DateTimeField(null=True, blank=True, help_text="First time packet was downloaded")
    download_count = models.IntegerField(default=0, help_text="Number of times downloaded")
    last_downloaded_at = models.DateTimeField(null=True, blank=True)
    downloaded_by = models.CharField(max_length=255, blank=True, help_text="Username who last downloaded")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["team__team_number"]
        unique_together = [["packet", "team"]]
        verbose_name = "Packet Distribution"
        verbose_name_plural = "Packet Distributions"
        indexes = [
            models.Index(fields=["packet", "email_status"]),
            models.Index(fields=["packet", "team"]),
        ]

    def __str__(self) -> str:
        return f"{self.packet.title} for Team {self.team.team_number} ({self.email_status})"

    def mark_as_sent(self, email: str) -> None:
        self.email_status = "sent"
        self.email_sent_to = email
        self.email_sent_at = timezone.now()
        self.save(
            update_fields=[
                "email_status",
                "email_sent_to",
                "email_sent_at",
                "updated_at",
            ]
        )

    def mark_as_failed(self, error_message: str) -> None:
        self.email_status = "failed"
        self.email_error_message = error_message
        self.save(update_fields=["email_status", "email_error_message", "updated_at"])

    def record_download(self, username: str) -> None:
        """Record a packet download (one UPDATE, so concurrent downloads all count)."""
        now = timezone.now()
        PacketDistribution.objects.filter(pk=self.pk).update(
            downloaded_at=Coalesce("downloaded_at", Value(now)),
            download_count=F("download_count") + 1,
            last_downloaded_at=now,
            downloaded_by=username,
            updated_at=now,
        )
        self.refresh_from_db(
            fields=["downloaded_at", "download_count", "last_downloaded_at", "downloaded_by", "updated_at"]
        )
