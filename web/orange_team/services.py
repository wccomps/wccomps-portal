from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from scoring.models import OrangeTeamScore

from orange_team.forms import CriterionInput
from orange_team.models import (
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
)
from team.models import Team


def auto_assign_check(
    check: OrangeCheck,
    checked_in_users: list[User],
    teams: list[Team],
    rotation_offset: int = 0,
) -> int:
    """Assign on-shift volunteers to teams for a check, rotated by rotation_offset.

    Submitted/approved cells are left untouched (locked to their scorer); pending or
    missing cells are (re)assigned round-robin. Returns the number of cells changed.
    """
    if not checked_in_users:
        return 0
    n = len(checked_in_users)
    criteria = list(check.criteria.all())
    count = 0
    with transaction.atomic():
        existing = {a.team_id: a for a in check.assignments.select_for_update()}
        for j, team in enumerate(sorted(teams, key=lambda t: t.team_number)):
            current = existing.get(team.id)
            if current is not None and current.status in ("submitted", "approved"):
                continue
            assignee = checked_in_users[(j + rotation_offset) % n]
            if current is not None:
                if current.user_id != assignee.id:
                    current.user = assignee
                    current.save(update_fields=["user"])
                    count += 1
                continue
            assignment = OrangeAssignment.objects.create(orange_check=check, user=assignee, team=team)
            OrangeAssignmentResult.objects.bulk_create(
                OrangeAssignmentResult(assignment=assignment, criterion=c) for c in criteria
            )
            count += 1
    return count


def rebalance_unstarted(check: OrangeCheck, checked_in_users: list[User]) -> int:
    """Redistribute only pending assignments evenly across the given volunteers."""
    if not checked_in_users:
        return 0
    n = len(checked_in_users)
    count = 0
    with transaction.atomic():
        pending = list(
            check.assignments.select_for_update().filter(status="pending").order_by("team__team_number")
        )
        for i, assignment in enumerate(pending):
            assignee = checked_in_users[i % n]
            if assignment.user_id != assignee.id:
                assignment.user = assignee
                assignment.save(update_fields=["user"])
                count += 1
    return count


def assign_all_checks(
    checks: list[OrangeCheck],
    checked_in_users: list[User],
    teams: list[Team],
) -> int:
    total = 0
    for offset, check in enumerate(checks):
        total += auto_assign_check(check, checked_in_users, teams, rotation_offset=offset)
    return total


def update_check_criteria(check: OrangeCheck, criteria: list[CriterionInput]) -> None:
    """Apply an edited rubric in place, so grading already done on kept criteria survives.

    Posted rows with a known id are updated; rows without one are created, with an unmet
    result on every existing assignment so they can be graded; criteria left out are
    deleted along with their results.
    """
    with transaction.atomic():
        existing = {c.pk: c for c in check.criteria.select_for_update()}
        posted_ids = {c["id"] for c in criteria if c["id"] in existing}
        check.criteria.exclude(pk__in=posted_ids).delete()

        assignments = list(check.assignments.all())
        for c in criteria:
            criterion = existing.get(c["id"]) if c["id"] is not None else None
            if criterion is not None:
                criterion.label = c["label"]
                criterion.points = c["points"]
                criterion.sort_order = c["sort_order"]
                criterion.save(update_fields=["label", "points", "sort_order"])
                continue
            criterion = OrangeCheckCriterion.objects.create(
                orange_check=check, label=c["label"], points=c["points"], sort_order=c["sort_order"]
            )
            OrangeAssignmentResult.objects.bulk_create(
                OrangeAssignmentResult(assignment=a, criterion=criterion) for a in assignments
            )


def create_orange_score_from_assignment(
    assignment: OrangeAssignment,
    approver: User,
) -> OrangeTeamScore:
    """Create an OrangeTeamScore record from an approved orange team check."""
    return OrangeTeamScore.objects.create(
        team=assignment.team,
        submitted_by=assignment.user,
        description=f"Check: {assignment.orange_check.title}",
        points_awarded=assignment.score or 0,
        is_approved=True,
        approved_by=approver,
        approved_at=timezone.now(),
        orange_check=assignment.orange_check,
    )
