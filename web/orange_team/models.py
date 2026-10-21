from datetime import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

# The most a check, or one of its criteria, is worth
MAX_POINTS = 10_000


class OrangeCheckIn(models.Model):
    """Tracks orange team member availability during competition."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orange_checkins")
    checked_in_at = models.DateTimeField(auto_now_add=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "orange_checkin"
        ordering = ["-checked_in_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_active=True),
                name="one_active_checkin_per_user",
            ),
        ]

    def __str__(self) -> str:
        status = "IN" if self.is_active else "OUT"
        return f"{self.user.username} [{status}]"

    @classmethod
    def toggle(cls, user: User) -> None:
        """Check the user out if currently checked in, or check them in."""
        with transaction.atomic():
            active = cls.objects.select_for_update().filter(user=user, is_active=True).first()
            if active:
                active.is_active = False
                active.checked_out_at = timezone.now()
                active.save(update_fields=["is_active", "checked_out_at"])
                return
            try:
                with transaction.atomic():
                    cls.objects.create(user=user)
            except IntegrityError:
                pass


class OrangeCheck(models.Model):
    """A check template with rubric criteria, created by a lead."""

    title = models.CharField(max_length=200)
    description = models.TextField(help_text="Steps/instructions for the orange teamer")
    scheduled_at = models.DateTimeField(null=True, blank=True, help_text="When assignments go live")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="created_checks")
    created_at = models.DateTimeField(auto_now_add=True)
    max_points = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(MAX_POINTS)], help_text="Max score when the check has no criteria"
    )
    time_limit = models.DurationField(
        null=True, blank=True, help_text="How long the window stays open from scheduled_at"
    )

    class Meta:
        db_table = "orange_check"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        # Its criteria (when it has any) are what it's worth; their own clean checks a change to them
        stored = self.criteria.aggregate(total=models.Sum("points"))["total"] if self.pk else None
        if problem := scores_total_problem(self.pk, self.max_points if stored is None else int(stored)):
            raise ValidationError(problem)

    @property
    def max_score(self) -> int:
        criteria_total = self.criteria.aggregate(total=models.Sum("points"))["total"]
        if criteria_total is not None:
            return int(criteria_total)
        return self.max_points

    @property
    def window_end(self) -> datetime | None:
        if self.scheduled_at is None or self.time_limit is None:
            return None
        return self.scheduled_at + self.time_limit

    def is_open(self, now: datetime) -> bool:
        end = self.window_end
        return end is not None and self.scheduled_at is not None and self.scheduled_at <= now < end

    def is_upcoming(self, now: datetime) -> bool:
        return self.scheduled_at is not None and now < self.scheduled_at

    def is_closed(self, now: datetime) -> bool:
        end = self.window_end
        return end is not None and now >= end

    @property
    def window_state(self) -> str:
        """The check's scheduling state for display: open / upcoming / closed / unscheduled."""
        now = timezone.now()
        if self.is_open(now):
            return "open"
        if self.is_upcoming(now):
            return "upcoming"
        if self.is_closed(now):
            return "closed"
        return "unscheduled"


def scores_total_problem(check_pk: int | None, max_score: int) -> str:
    """Why the checks' scores, with this check (None: a new one) worth max_score, can't all be counted, or ''.

    The scoring sums every check's maximum into ScoringTemplate.orange_max, a numeric column of limited size.
    """
    from scoring.models import ScoringTemplate

    field = ScoringTemplate._meta.get_field("orange_max")
    capacity = 10 ** (getattr(field, "max_digits", 0) - getattr(field, "decimal_places", 0)) - 1
    others = OrangeCheck.objects.all() if check_pk is None else OrangeCheck.objects.exclude(pk=check_pk)
    others = others.annotate(criteria_total=models.Sum("criteria__points"))
    total = max_score + sum(t if t is not None else m for m, t in others.values_list("max_points", "criteria_total"))
    if total > capacity:
        return f"All checks together would be worth {total} points; the scoring can count at most {capacity}."
    return ""


class OrangeCheckCriterion(models.Model):
    """A rubric line item for a check."""

    orange_check = models.ForeignKey(OrangeCheck, on_delete=models.CASCADE, related_name="criteria")
    label = models.CharField(max_length=200)
    points = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(MAX_POINTS)])
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "orange_check_criterion"
        ordering = ["sort_order", "id"]

    def clean(self) -> None:
        if not self.orange_check_id:  # a rubric row the check form checks before the check exists
            return
        others = self.orange_check.criteria.exclude(pk=self.pk).aggregate(total=models.Sum("points"))["total"] or 0
        if problem := scores_total_problem(self.orange_check_id, int(others) + self.points):
            raise ValidationError(problem)

    def __str__(self) -> str:
        return f"{self.label} ({self.points} pts)"


class OrangeAssignmentQuerySet(models.QuerySet["OrangeAssignment"]):
    def unfinished(self) -> OrangeAssignmentQuerySet:
        """Assignments still to score or review: approved and rejected ones are done."""
        return self.exclude(status__in=OrangeAssignment.FINISHED_STATUSES)


class OrangeAssignment(models.Model):
    """Assignment of an orange teamer to score a specific team on a check."""

    objects = OrangeAssignmentQuerySet.as_manager()
    FINISHED_STATUSES = ("approved", "rejected")

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("in_progress", "In Progress"),
        ("submitted", "Submitted"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    orange_check = models.ForeignKey(OrangeCheck, on_delete=models.CASCADE, related_name="assignments")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orange_assignments")
    team = models.ForeignKey("team.Team", on_delete=models.CASCADE, related_name="orange_assignments")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    score = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Reviewer notes (for rejections)")
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_assignments"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "orange_assignment"
        constraints = [
            models.UniqueConstraint(fields=["orange_check", "team"], name="one_assignment_per_team_per_check"),
        ]
        ordering = ["team__team_number"]

    def __str__(self) -> str:
        return f"{self.orange_check.title} for Team {self.team.team_number} ({self.user.username})"

    def calculate_score(self) -> int:
        return self.results.filter(met=True).aggregate(total=models.Sum("criterion__points"))["total"] or 0


class OrangeAssignmentResult(models.Model):
    """Per-criterion result for an assignment."""

    assignment = models.ForeignKey(OrangeAssignment, on_delete=models.CASCADE, related_name="results")
    criterion = models.ForeignKey(OrangeCheckCriterion, on_delete=models.CASCADE, related_name="results")
    met = models.BooleanField(default=False)

    class Meta:
        db_table = "orange_assignment_result"
        constraints = [
            models.UniqueConstraint(fields=["assignment", "criterion"], name="one_result_per_criterion_per_assignment"),
        ]

    def __str__(self) -> str:
        status = "MET" if self.met else "NOT MET"
        return f"{self.criterion.label}: {status}"


class OrangeFollowUp(models.Model):
    """Personal reminder for an orange teamer to revisit a team."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orange_followups")
    assignment = models.ForeignKey(OrangeAssignment, on_delete=models.CASCADE, related_name="followups")
    remind_at = models.DateTimeField()
    note = models.TextField(blank=True)
    dismissed = models.BooleanField(default=False)

    class Meta:
        db_table = "orange_followup"
        ordering = ["remind_at"]

    def __str__(self) -> str:
        return f"Reminder for {self.user.username} at {self.remind_at}"
