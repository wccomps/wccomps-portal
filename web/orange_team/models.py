from datetime import datetime

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


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


class OrangeCheck(models.Model):
    """A check template with rubric criteria, created by a lead."""

    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("scheduled", "Scheduled"),
        ("active", "Active"),
        ("closed", "Closed"),
    ]

    title = models.CharField(max_length=200)
    description = models.TextField(help_text="Steps/instructions for the orange teamer")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="draft")
    scheduled_at = models.DateTimeField(null=True, blank=True, help_text="When assignments go live")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="created_checks")
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    max_points = models.PositiveIntegerField(default=0, help_text="Max score when the check has no criteria")
    time_limit = models.DurationField(
        null=True, blank=True, help_text="How long the window stays open from scheduled_at"
    )

    class Meta:
        db_table = "orange_check"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.status}] {self.title}"

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


class OrangeCheckCriterion(models.Model):
    """A rubric line item for a check."""

    orange_check = models.ForeignKey(OrangeCheck, on_delete=models.CASCADE, related_name="criteria")
    label = models.CharField(max_length=200)
    points = models.PositiveIntegerField()
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "orange_check_criterion"
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"{self.label} ({self.points} pts)"


class OrangeAssignment(models.Model):
    """Assignment of an orange teamer to score a specific team on a check."""

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
        return f"{self.orange_check.title} - Team {self.team.team_number} ({self.user.username})"

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
