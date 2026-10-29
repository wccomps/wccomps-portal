from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

# The most a check, or one of its criteria, is worth
MAX_POINTS = 10_000
# The previous release writes the checks' sum to scoringtemplate.orange_max, numeric(10, 2), until that column goes
MAX_TOTAL_POINTS = 99_999_999


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


def _max_score() -> Coalesce:
    """check_worth as an expression over a check."""
    criteria_total = (
        OrangeCheckCriterion.objects.filter(orange_check=models.OuterRef("pk"))
        .values("orange_check")
        .annotate(total=models.Sum("points"))
        .values("total")
    )
    return Coalesce(models.Subquery(criteria_total), models.F("max_points"), output_field=models.IntegerField())


class OrangeCheckQuerySet(models.QuerySet["OrangeCheck"]):
    def with_max_score(self) -> OrangeCheckQuerySet:
        """Each check with its maximum, which max_score then reads without a query."""
        return self.annotate(annotated_max_score=_max_score())

    def total_max(self) -> int:
        return sum(check.max_score for check in self.with_max_score())


class OrangeCheck(models.Model):
    """A check template with rubric criteria, created by a lead."""

    objects = OrangeCheckQuerySet.as_manager()

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
        if problem := scores_total_problem(self.pk, check_worth(self._criteria_points(), self.max_points)):
            raise ValidationError(problem)

    @property
    def max_score(self) -> int:
        annotated = self.__dict__.get("annotated_max_score")
        if annotated is not None:
            return int(annotated)
        return check_worth(self._criteria_points(), self.max_points)

    def _criteria_points(self) -> list[int]:
        return [] if self._state.adding else list(self.criteria.values_list("points", flat=True))

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


def check_worth(criteria_points: Sequence[int], max_points: int) -> int:
    """What a check is worth: its criteria's points summed, or max_points when it has no criteria."""
    return sum(criteria_points) if criteria_points else max_points


def scores_total_problem(check_pk: int | None, max_score: int) -> str:
    """Why the checks' scores, with this check (None: a new one) worth max_score, can't all be counted, or ''.

    All checks together are limited to MAX_TOTAL_POINTS while the previous release can still store their sum.
    """
    others = OrangeCheck.objects.all() if check_pk is None else OrangeCheck.objects.exclude(pk=check_pk)
    total = max_score + others.total_max()
    if total > MAX_TOTAL_POINTS:
        return (
            f"All checks together would be worth {total} points; together they can be worth at most {MAX_TOTAL_POINTS}."
        )
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
        others = list(self.orange_check.criteria.exclude(pk=self.pk).values_list("points", flat=True))
        worth = check_worth([*others, self.points], self.orange_check.max_points)
        if problem := scores_total_problem(self.orange_check_id, worth):
            raise ValidationError(problem)

    def __str__(self) -> str:
        return f"{self.label} ({self.points} pts)"


Action = Literal["save", "submit", "reassign", "approve", "reject"]


class OrangeAssignmentQuerySet(models.QuerySet["OrangeAssignment"]):
    def unfinished(self) -> OrangeAssignmentQuerySet:
        """Assignments still on the teamer's dashboard (OrangeAssignment.STATUSES)."""
        return self.exclude(status__in=OrangeAssignment.FINISHED_STATUSES)

    def locked_for(self, action: Action, pk: int) -> tuple[OrangeAssignment, bool]:
        """Lock the assignment (DoesNotExist when this queryset hasn't it) and move it by action, unsaved, when its
        status allows action; whether it did. Call it in a transaction."""
        assignment = self.select_for_update(of=("self",)).get(pk=pk)
        moved_to = assignment.status_info.moves.get(action)
        if moved_to is not None:
            assignment.status = moved_to
        return assignment, moved_to is not None


@dataclass(frozen=True)
class AssignmentStatus:
    label: str
    on_dashboard: bool
    in_review_queue: bool
    scored: bool  # its score stands: exported, counted as scored, kept in step with a rubric edit
    unstarted: bool  # auto-assign and rebalance may hand it to another volunteer
    returned: bool  # sent back to the volunteer by the lead, whose notes say why
    moves: Mapping[Action, str]


class OrangeAssignment(models.Model):
    """Assignment of an orange teamer to score a specific team on a check."""

    objects = OrangeAssignmentQuerySet.as_manager()

    STATUSES: Mapping[str, AssignmentStatus] = {
        "pending": AssignmentStatus(
            "Pending",
            True,
            False,
            False,
            True,
            False,
            {"save": "in_progress", "submit": "submitted", "reassign": "pending"},
        ),
        "in_progress": AssignmentStatus(
            "In Progress",
            True,
            True,
            False,
            False,
            False,
            {"save": "in_progress", "submit": "submitted", "reassign": "in_progress"},
        ),
        "submitted": AssignmentStatus(
            "Submitted", True, True, True, False, False, {"approve": "approved", "reject": "rejected"}
        ),
        "approved": AssignmentStatus("Approved", False, False, True, False, False, {}),
        "rejected": AssignmentStatus(
            "Rejected",
            True,
            False,
            False,
            False,
            True,
            {"save": "rejected", "submit": "submitted", "reassign": "rejected"},
        ),
    }
    STATUS_CHOICES = [(status, row.label) for status, row in STATUSES.items()]
    FINISHED_STATUSES = tuple(status for status, row in STATUSES.items() if not row.on_dashboard)
    REVIEW_STATUSES = tuple(status for status, row in STATUSES.items() if row.in_review_queue)
    SCORED_STATUSES = tuple(status for status, row in STATUSES.items() if row.scored)

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

    @property
    def status_info(self) -> AssignmentStatus:
        return self.STATUSES[self.status]

    @property
    def actions(self) -> frozenset[str]:
        """What its status allows, for templates."""
        return frozenset(self.status_info.moves)

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
