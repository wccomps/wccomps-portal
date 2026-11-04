import random

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


def _lock_check(check: OrangeCheck) -> None:
    """Hold the check row for the transaction: the one point auto-assign and a rubric edit order on."""
    list(OrangeCheck.objects.select_for_update().filter(pk=check.pk).values_list("pk", flat=True))


def auto_assign_check(
    check: OrangeCheck,
    checked_in_users: list[User],
    teams: list[Team],
) -> int:
    if not checked_in_users:
        return 0
    n = len(checked_in_users)
    count = 0
    with transaction.atomic():
        _lock_check(check)
        criteria = list(check.criteria.all())
        existing = {a.team_id: a for a in check.assignments.select_for_update().order_by("pk")}
        assignable = [
            team for team in teams if (current := existing.get(team.id)) is None or current.status_info.unstarted
        ]
        shuffled_teams = random.sample(assignable, len(assignable))
        shuffled_users = random.sample(checked_in_users, n)
        for j, team in enumerate(shuffled_teams):
            current = existing.get(team.id)
            assignee = shuffled_users[j % n]
            if current is not None:
                if current.user_id != assignee.id:
                    current.user = assignee
                    current.save(update_fields=["user"])
                    count += 1
                continue
            # get_or_create (not create): a concurrent run or double-submit that already
            # inserted this (check, team) resolves to one row instead of an IntegrityError.
            assignment, created = OrangeAssignment.objects.get_or_create(
                orange_check=check, team=team, defaults={"user": assignee}
            )
            if created:
                OrangeAssignmentResult.objects.bulk_create(
                    OrangeAssignmentResult(assignment=assignment, criterion=c) for c in criteria
                )
                count += 1
    return count


def rebalance_unstarted(check: OrangeCheck, checked_in_users: list[User]) -> int:
    if not checked_in_users:
        return 0
    n = len(checked_in_users)
    count = 0
    with transaction.atomic():
        locked = check.assignments.select_for_update(of=("self",)).order_by("pk")
        unstarted = [a for a in locked if a.status_info.unstarted]
        shuffled_assignments = random.sample(unstarted, len(unstarted))
        shuffled_users = random.sample(checked_in_users, n)
        for i, assignment in enumerate(shuffled_assignments):
            assignee = shuffled_users[i % n]
            if assignment.user_id != assignee.id:
                assignment.user = assignee
                assignment.save(update_fields=["user"])
                count += 1
    return count


def update_check_criteria(check: OrangeCheck, criteria: list[CriterionInput]) -> None:
    """Apply an edited rubric in place, so grading already done on kept criteria survives."""
    with transaction.atomic():
        # The check, then its assignments, then criteria and results: auto-assign and autosave lock in this order
        _lock_check(check)
        assignments = list(check.assignments.select_for_update().order_by("pk"))
        existing = {c.pk: c for c in check.criteria.select_for_update()}
        posted_ids = {c["id"] for c in criteria if c["id"] in existing}
        check.criteria.exclude(pk__in=posted_ids).delete()
        for c in criteria:
            criterion = existing.get(c["id"]) if c["id"] is not None else None
            if criterion is not None:
                criterion.label = c["label"]
                criterion.points = c["points"]
                criterion.criterion_type = c["criterion_type"]
                criterion.sort_order = c["sort_order"]
                criterion.save(update_fields=["label", "points", "criterion_type", "sort_order"])
                criterion.reconcile_results()
                continue
            criterion = OrangeCheckCriterion.objects.create(
                orange_check=check,
                label=c["label"],
                points=c["points"],
                criterion_type=c["criterion_type"],
                sort_order=c["sort_order"],
            )
            OrangeAssignmentResult.objects.bulk_create(
                OrangeAssignmentResult(assignment=a, criterion=criterion) for a in assignments
            )

        has_criteria = check.criteria.exists()
        for assignment in assignments:
            if not assignment.status_info.unstarted:
                if has_criteria:
                    assignment.score = assignment.calculate_score()
                    assignment.save(update_fields=["score"])
                elif assignment.score is not None and assignment.score > check.max_score:
                    assignment.score = check.max_score
                    assignment.save(update_fields=["score"])
                if assignment.status == "approved":
                    OrangeTeamScore.objects.filter(
                        orange_check=check,
                        team=assignment.team,
                    ).update(
                        points_awarded=assignment.score or 0,
                    )
        OrangeTeamScore.objects.filter(orange_check=check).update(description=f"Check: {check.title}")


def approve_assignment(
    assignment: OrangeAssignment,
    approver: User,
) -> OrangeTeamScore:
    assignment.status = "approved"
    assignment.reviewed_by = approver
    assignment.reviewed_at = timezone.now()
    assignment.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    score, _ = OrangeTeamScore.objects.update_or_create(
        orange_check=assignment.orange_check,
        team=assignment.team,
        defaults={
            "submitted_by": assignment.user,
            "description": f"Check: {assignment.orange_check.title}",
            "points_awarded": assignment.score or 0,
            "is_approved": True,
            "approved_by": approver,
            "approved_at": timezone.now(),
        },
    )
    return score
