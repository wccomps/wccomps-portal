import secrets
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import models, transaction
from django.utils import timezone


def generate_edit_token() -> str:
    return secrets.token_urlsafe(48)


def collapse_whitespace(name: str) -> str:
    return " ".join(name.split())


def normalize_school_name(name: str) -> str:
    """The key a school is matched on: case and runs of whitespace don't matter."""
    return collapse_whitespace(name).casefold()


class School(models.Model):
    """A school across events. Season history attaches here, never to a team number (they're reshuffled)."""

    name = models.CharField(max_length=255)
    name_key = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def for_name(cls, name: str) -> School:
        """The school with this name (ignoring case and spacing), created if new; keeps the latest spelling."""
        name = collapse_whitespace(name)
        school, created = cls.objects.get_or_create(name_key=normalize_school_name(name), defaults={"name": name})
        if not created and school.name != name:
            school.name = name
            school.save(update_fields=["name"])
        return school


class TeamRegistration(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved"),
        ("paid", "Paid"),
        ("credentials_sent", "Credentials Sent"),
        ("rejected", "Rejected"),
    ]

    REGION_CHOICES = [
        ("wrccdc", "Western Regional (WRCCDC)"),
        ("prccdc", "Pacific Rim (PRCCDC)"),
        ("mwccdc", "Midwest (MWCCDC)"),
        ("rmccdc", "Rocky Mountain (RMCCDC)"),
        ("swccdc", "Southwest (SWCCDC)"),
        ("neccdc", "Northeast (NECCDC)"),
        ("maccdc", "Mid-Atlantic (MACCDC)"),
        ("seccdc", "Southeast (SECCDC)"),
        ("at_large", "At-Large"),
    ]

    school_name = models.CharField(max_length=255)
    region = models.CharField(max_length=20, choices=REGION_CHOICES, default="wrccdc")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    # Token for self-service editing
    edit_token = models.CharField(max_length=64, unique=True, default=generate_edit_token)
    edit_token_expires = models.DateTimeField(null=True, blank=True)

    submitted_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    credentials_sent_at = models.DateTimeField(null=True, blank=True)

    rejection_reason = models.TextField(blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    # Set when the registration's school is assigned a team at an event
    school = models.ForeignKey(School, null=True, blank=True, on_delete=models.SET_NULL, related_name="registrations")

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:
        return f"{self.school_name} ({self.status})"

    def approve(self, user: User) -> None:
        self.status = "approved"
        self.approved_at = timezone.now()
        self.approved_by = user
        self.save()

    def reject(self, reason: str) -> None:
        self.status = "rejected"
        self.rejection_reason = reason
        self.save()

    def mark_as_paid(self) -> None:
        self.status = "paid"
        self.paid_at = timezone.now()
        self.save()


class SeasonManager(models.Manager["Season"]):
    def active(self) -> Season | None:
        return self.filter(is_active=True).first()


class Season(models.Model):
    """Competition season (e.g., 2026 Season)."""

    objects = SeasonManager()

    name = models.CharField(max_length=100)
    year = models.IntegerField(unique=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year"]

    def __str__(self) -> str:
        return self.name


class EventQuerySet(models.QuerySet["Event"]):
    def annotate_enrollment_count(self) -> EventQuerySet:
        return self.annotate(enrollment_count=models.Count("enrollments"))


class EventManager(models.Manager["Event"]):
    def get_queryset(self) -> EventQuerySet:
        return EventQuerySet(self.model, using=self._db)

    def annotate_enrollment_count(self) -> EventQuerySet:
        return self.get_queryset().annotate_enrollment_count()

    def open(self) -> Event | None:
        """The active event (the database allows one); packets sent now belong to it."""
        return self.filter(is_active=True).first()


class Event(models.Model):
    EVENT_TYPE_CHOICES = [
        ("invitational", "Invitational"),
        ("qualifier", "Qualifier"),
        ("regional", "Regional"),
        ("state", "State"),
    ]

    season = models.ForeignKey(Season, on_delete=models.CASCADE, related_name="events")
    objects = EventManager()
    name = models.CharField(max_length=255)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    event_number = models.IntegerField(null=True, blank=True)
    date = models.DateField()
    start_time = models.TimeField(default="09:00")
    end_time = models.TimeField(default="17:00")
    registration_open = models.BooleanField(default=True)
    registration_deadline = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=False)
    is_finalized = models.BooleanField(default=False)
    max_teams = models.IntegerField(default=50)

    reminder_days = models.JSONField(default=list)
    last_reminder_sent = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["season", "date"]
        constraints = [
            models.UniqueConstraint(
                fields=["season", "event_type", "event_number"],
                name="unique_event_per_season",
            ),
            models.UniqueConstraint(fields=["is_active"], condition=models.Q(is_active=True), name="one_open_event"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.season.name})"

    @transaction.atomic
    def open(self) -> None:
        """Make this the only active event."""
        Event.objects.exclude(pk=self.pk).filter(is_active=True).update(is_active=False)
        if not self.is_active:
            self.is_active = True
            self.save(update_fields=["is_active", "updated_at"])


class RegistrationContact(models.Model):
    ROLE_CHOICES = [
        ("captain", "Team Captain"),
        ("co_captain", "Co-Captain"),
        ("coach", "Coach/Faculty Advisor"),
        ("site_judge", "Site Judge"),
    ]

    registration = models.ForeignKey(TeamRegistration, on_delete=models.CASCADE, related_name="contacts")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    name = models.CharField(max_length=255)
    email = models.EmailField()
    phone = models.CharField(max_length=50, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "role"],
                name="unique_contact_role_per_registration",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_role_display()}) - {self.registration.school_name}"


class RegistrationEventEnrollment(models.Model):
    registration = models.ForeignKey(TeamRegistration, on_delete=models.CASCADE, related_name="event_enrollments")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="enrollments")
    enrolled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["registration", "event"],
                name="unique_enrollment_per_registration",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.registration.school_name} → {self.event.name}"


class EventTeamAssignment(models.Model):
    """Per-event random team number assignment."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="team_assignments")
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name="assignments")
    # Only for teams assigned through the registration pages; imported schools have none
    registration = models.ForeignKey(
        TeamRegistration, null=True, blank=True, on_delete=models.SET_NULL, related_name="team_assignments"
    )
    team = models.ForeignKey("team.Team", on_delete=models.CASCADE, related_name="event_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)
    credentials_sent_at = models.DateTimeField(null=True, blank=True)
    password_generated = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "school"], name="unique_assignment_per_school"),
            models.UniqueConstraint(fields=["event", "team"], name="unique_team_per_event"),
        ]

    def __str__(self) -> str:
        return f"{self.school.name} → Team {self.team.team_number:02d} ({self.event.name})"


class RegistrationRateLimit(models.Model):
    """One successful public registration submission, for per-client rate limiting.

    Stored in the database so every gunicorn worker shares the count.
    """

    LIMIT = 10
    WINDOW = timedelta(hours=1)

    ip = models.GenericIPAddressField(unpack_ipv4=True)
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["ip", "-attempted_at"]),
        ]

    def __str__(self) -> str:
        return f"Registration from {self.ip} at {self.attempted_at}"

    @classmethod
    def over_limit(cls, ip: str) -> bool:
        """Whether this client has used up its submissions for the current window."""
        since = timezone.now() - cls.WINDOW
        return cls.objects.filter(ip=ip, attempted_at__gte=since).count() >= cls.LIMIT

    @classmethod
    def record(cls, ip: str) -> None:
        """Count a submission, pruning rows that have aged out of every window."""
        cls.objects.filter(attempted_at__lt=timezone.now() - cls.WINDOW).delete()
        cls.objects.create(ip=ip)
