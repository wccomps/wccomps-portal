import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import TypedDict

from django.contrib.auth.models import User
from django.db import connection, models, transaction
from django.db.models.functions import Now
from django.utils import timezone
from django_stubs_ext import WithAnnotations

from core.utils import IPAddress

from .locks import school_list_lock


def collapse_whitespace(name: str) -> str:
    return " ".join(name.split())


def normalize_school_name(name: str) -> str:
    """The key a school is matched on: case and runs of whitespace don't matter."""
    return collapse_whitespace(name).casefold()


SCHOOL_NAME_MAX_LENGTH = 255  # School.name and the names it is made from


class School(models.Model):
    """A school across events, matched by name_key."""

    name = models.CharField(max_length=SCHOOL_NAME_MAX_LENGTH)
    # Unlimited: case folding lengthens some names (ß matches ss), so a key can be longer than its name
    name_key = models.TextField(unique=True)
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

    school_name = models.CharField(max_length=SCHOOL_NAME_MAX_LENGTH)
    region = models.CharField(max_length=20, choices=REGION_CHOICES, default="wrccdc")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    submitted_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    rejection_reason = models.TextField(blank=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    # Set when the registration is assigned a team through registration.services
    school = models.ForeignKey(School, null=True, blank=True, on_delete=models.SET_NULL, related_name="registrations")

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:
        return f"{self.school_name} ({self.status})"

    @property
    def primary_contact(self) -> RegistrationContact | None:
        """The captain, else the first contact; None when it has none. Reads contacts.all(), so a prefetch serves it."""
        contacts = sorted(self.contacts.all(), key=lambda contact: (contact.role != "captain", contact.pk))
        return contacts[0] if contacts else None

    def approve(self, user: User) -> bool:
        """Approve it if it is still pending; False when it no longer is."""
        return self._decide(status="approved", approved_at=timezone.now(), approved_by=user)

    def reject(self, reason: str) -> bool:
        """Reject it if it is still pending; False when it no longer is."""
        return self._decide(status="rejected", rejection_reason=reason)

    def _decide(self, **fields: object) -> bool:
        # One conditional update, so two reviews racing on the same registration can't both take
        decided = TeamRegistration.objects.filter(pk=self.pk, status="pending").update(**fields) == 1
        if decided:
            for name, value in fields.items():
                setattr(self, name, value)
        return decided


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

    @transaction.atomic
    def activate(self) -> None:
        """Make this the only active season, under the school list lock. The only writer of is_active: a season
        stops being active when another is activated."""
        school_list_lock()
        Season.objects.exclude(pk=self.pk).filter(is_active=True).update(is_active=False)
        Season.objects.filter(pk=self.pk).update(is_active=True)
        self.is_active = True


class RegistrationOpen(TypedDict):
    registration_is_open: bool


class EventManager(models.Manager["Event"]):
    def open(self) -> Event | None:
        """The active event (the database allows one), with its season."""
        return self.filter(is_active=True).select_related("season").first()

    @staticmethod
    def _open_for_registration() -> models.Q:
        """Open for registration: accepting registrations, in the active season, not closed out, and before its
        deadline when it has one. The deadline is compared with the database clock when the query runs."""
        return models.Q(registration_open=True, season__is_active=True, is_finalized=False) & (
            models.Q(registration_deadline__isnull=True) | models.Q(registration_deadline__gt=Now())
        )

    def open_for_registration(self) -> models.QuerySet[Event]:
        return self.filter(self._open_for_registration())

    def with_registration_open(self) -> models.QuerySet[WithAnnotations[Event, RegistrationOpen]]:
        return self.annotate(
            registration_is_open=models.ExpressionWrapper(
                self._open_for_registration(), output_field=models.BooleanField()
            )
        )


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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["season", "date"]
        constraints = [
            models.UniqueConstraint(
                fields=["season", "event_type", "event_number"],
                name="unique_event_per_season",
                violation_error_message="This season already has an event of this type with this number.",
            ),
            models.UniqueConstraint(fields=["is_active"], condition=models.Q(is_active=True), name="one_open_event"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.season.name})"

    @transaction.atomic
    def open(self) -> None:
        """Make this the only active event, under the school list lock. With close, the only writer of is_active."""
        school_list_lock()
        Event.objects.exclude(pk=self.pk).filter(is_active=True).update(is_active=False)
        if not self.is_active:
            self.is_active = True
            self.save(update_fields=["is_active", "updated_at"])

    @transaction.atomic
    def close(self) -> None:
        """Make this event inactive, under the school list lock."""
        school_list_lock()
        self.is_active = False
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
        return f"{self.name} ({self.get_role_display()}), {self.registration.school_name}"


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
        return f"{self.registration.school_name} in {self.event.name}"


class EventTeamAssignment(models.Model):
    """Which team number a school held at an event; imported schools have no registration."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="team_assignments")
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name="assignments")
    # Only for teams assigned through the registration pages; imported schools have none
    registration = models.ForeignKey(
        TeamRegistration, null=True, blank=True, on_delete=models.SET_NULL, related_name="team_assignments"
    )
    team = models.ForeignKey("team.Team", on_delete=models.CASCADE, related_name="event_assignments")
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "school"], name="unique_assignment_per_school"),
            models.UniqueConstraint(fields=["event", "team"], name="unique_team_per_event"),
        ]

    def __str__(self) -> str:
        return f"{self.school.name} as Team {self.team.team_number:02d} ({self.event.name})"


class EventResult(models.Model):
    """A school's result at a finalized event: its scorecard frozen as JSON (scoring.snapshot)."""

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="results")
    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name="results")
    team_number = models.PositiveSmallIntegerField()
    rank = models.PositiveSmallIntegerField(null=True, blank=True)  # None: excluded or no scoring activity
    total_score = models.DecimalField(max_digits=12, decimal_places=2)
    scorecard = models.JSONField()
    version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = [models.F("rank").asc(nulls_last=True), "team_number"]
        constraints = [
            models.UniqueConstraint(fields=["event", "school"], name="one_result_per_school_per_event"),
        ]

    def __str__(self) -> str:
        rank = f": #{self.rank}" if self.rank else ""
        return f"{self.school.name} at {self.event.name}{rank}"


class EventArchive(models.Model):
    """The event's full export zip, for whatever the scorecard didn't keep; written at Close Out."""

    event = models.OneToOneField(Event, on_delete=models.PROTECT, related_name="archive")
    data = models.BinaryField()
    updated_at = models.DateTimeField(auto_now=True)


class RegistrationRateLimit(models.Model):
    """One successful public registration submission, for per-client rate limiting.

    Stored in the database so every gunicorn worker shares the count.
    """

    LIMIT = 10
    # Per IPv6 /48, a site's whole allocation: rotating through its 65,536 /64s mustn't multiply LIMIT by as many
    NETWORK_LIMIT = 5 * LIMIT
    # Per IP version, each (prefix length, limit): a client is over if any of its networks is
    TIERS = {4: ((32, LIMIT),), 6: ((64, LIMIT), (48, NETWORK_LIMIT))}
    WINDOW = timedelta(hours=1)

    # The client's longest-prefix network: an IPv4 address, or an IPv6 /64 base
    ip = models.GenericIPAddressField(unpack_ipv4=True)
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["ip", "-attempted_at"]),
        ]

    def __str__(self) -> str:
        return f"Registration from {self.ip} at {self.attempted_at}"

    @classmethod
    @contextmanager
    def held(cls, ip: IPAddress) -> Iterator[bool]:
        """Serializes this client's widest-network check and record; yields whether it is over its limit."""
        widest = min(prefix for prefix, _ in cls.TIERS[ip.version])
        key = f"registration_rate:{ipaddress.ip_network((ip, widest), strict=False)}"
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
            yield cls.over_limit(ip)

    @classmethod
    def over_limit(cls, ip: IPAddress) -> bool:
        """Whether any of this client's networks has used up its submissions for the current window."""
        recent = cls.objects.filter(attempted_at__gte=timezone.now() - cls.WINDOW)
        return any(
            recent.filter(ip__in_network=str(ipaddress.ip_network((ip, prefix), strict=False))).count() >= limit
            for prefix, limit in cls.TIERS[ip.version]
        )

    @classmethod
    def record(cls, ip: IPAddress) -> None:
        """Count a submission, pruning rows that have aged out of every window."""
        cls.objects.filter(attempted_at__lt=timezone.now() - cls.WINDOW).delete()
        longest = max(prefix for prefix, _ in cls.TIERS[ip.version])
        cls.objects.create(ip=str(ipaddress.ip_network((ip, longest), strict=False).network_address))
