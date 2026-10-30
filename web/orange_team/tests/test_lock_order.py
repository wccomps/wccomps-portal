"""An autosave and a rubric edit lock an assignment before its results, so running together they can't deadlock."""

import json
import threading
import time
from collections.abc import Callable

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.db.models import QuerySet
from django.test import Client
from django.urls import reverse

from orange_team import views
from orange_team.models import OrangeAssignment, OrangeAssignmentResult, OrangeCheck, OrangeCheckCriterion
from orange_team.services import update_check_criteria
from team.models import Team

pytestmark = pytest.mark.django_db(transaction=True)


def test_an_autosave_and_a_rubric_edit_dropping_its_criterion_finish_together(
    create_user_with_groups: Callable[..., User],
) -> None:
    volunteer = create_user_with_groups("volunteer", ["WCComps_OrangeTeam"])
    check = OrangeCheck.objects.create(title="C", description="d")
    kept = OrangeCheckCriterion.objects.create(orange_check=check, label="Kept", points=2, sort_order=1)
    dropped = OrangeCheckCriterion.objects.create(orange_check=check, label="Dropped", points=3, sort_order=2)
    assignment = OrangeAssignment.objects.create(
        orange_check=check, user=volunteer, team=Team.objects.create(team_number=1, team_name="T1")
    )
    for criterion in (kept, dropped):
        OrangeAssignmentResult.objects.create(assignment=assignment, criterion=criterion)

    save_locked, edit_deleted = threading.Event(), threading.Event()
    failures: list[str] = []
    parse, delete = views._criterion_result, QuerySet.delete

    def parse_once_locked(body: bytes) -> tuple[int, bool] | None:
        save_locked.set()
        edit_deleted.wait(timeout=5)
        return parse(body)

    def delete_and_tell(self: QuerySet, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        deleted = delete(self, *args, **kwargs)
        if self.model is OrangeCheckCriterion:
            edit_deleted.set()
        return deleted

    def autosave() -> None:
        try:
            client = Client()
            client.force_login(volunteer)
            response = client.post(
                reverse("orange_team:assignment_save", args=[assignment.pk]),
                json.dumps({"criterion_id": dropped.pk, "met": True}),
                content_type="application/json",
            )
            if response.status_code >= 500:
                failures.append(f"autosave answered {response.status_code}")
        except Exception as e:
            failures.append(f"autosave: {e}")
        finally:
            connection.close()

    def edit() -> None:
        try:
            assert save_locked.wait(timeout=60)
            update_check_criteria(check, [{"id": kept.pk, "label": "Kept", "points": 2, "sort_order": 1}])
        except Exception as e:
            failures.append(f"rubric edit: {e}")
        finally:
            connection.close()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(views, "_criterion_result", parse_once_locked)
        patched.setattr(QuerySet, "delete", delete_and_tell)
        threads = [threading.Thread(target=autosave), threading.Thread(target=edit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert failures == []
    assert list(check.criteria.values_list("label", flat=True)) == ["Kept"]


def test_an_auto_assign_and_a_rubric_edit_of_the_same_check_finish_together(
    create_user_with_groups: Callable[..., User],
) -> None:
    """Auto-assign locks the check's assignments, then its new results take a key share on the criteria."""
    from orange_team.services import auto_assign_check

    volunteer = create_user_with_groups("volunteer", ["WCComps_OrangeTeam"])
    check = OrangeCheck.objects.create(title="C", description="d")
    criterion = OrangeCheckCriterion.objects.create(orange_check=check, label="Kept", points=2, sort_order=1)
    assigned = Team.objects.create(team_number=1, team_name="T1")
    unassigned = Team.objects.create(team_number=2, team_name="T2")
    OrangeAssignment.objects.create(orange_check=check, user=volunteer, team=assigned)

    assignments_locked = threading.Event()
    failures: list[str] = []
    results = OrangeAssignmentResult.objects
    bulk_create = results.bulk_create

    def bulk_create_once_the_edit_runs(*args: object, **kwargs: object) -> object:
        assignments_locked.set()
        time.sleep(2)  # the rubric edit reaches its locks
        return bulk_create(*args, **kwargs)

    def auto_assign() -> None:
        try:
            auto_assign_check(check, [volunteer], [assigned, unassigned])
        except Exception as e:
            failures.append(f"auto-assign: {e}")
        finally:
            connection.close()

    def edit() -> None:
        try:
            assert assignments_locked.wait(timeout=60)
            update_check_criteria(check, [{"id": criterion.pk, "label": "Kept", "points": 3, "sort_order": 1}])
        except Exception as e:
            failures.append(f"rubric edit: {e}")
        finally:
            connection.close()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(results, "bulk_create", bulk_create_once_the_edit_runs)
        threads = [threading.Thread(target=auto_assign), threading.Thread(target=edit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert failures == []
    assert OrangeAssignment.objects.filter(orange_check=check).count() == 2


def test_a_rubric_edit_racing_the_first_auto_assign_gives_every_assignment_every_criterion(
    create_user_with_groups: Callable[..., User],
) -> None:
    """A criterion added while the first auto-assign runs gets a result on the assignments it inserts."""
    from orange_team.services import auto_assign_check

    volunteer = create_user_with_groups("volunteer", ["WCComps_OrangeTeam"])
    check = OrangeCheck.objects.create(title="C", description="d")
    kept = OrangeCheckCriterion.objects.create(orange_check=check, label="Kept", points=2, sort_order=1)
    team = Team.objects.create(team_number=1, team_name="T1")

    criteria_read = threading.Event()
    failures: list[str] = []
    assignments = OrangeAssignment.objects
    get_or_create = assignments.get_or_create

    def get_or_create_once_the_edit_runs(*args: object, **kwargs: object) -> object:
        criteria_read.set()
        time.sleep(2)  # the rubric edit adds its criterion
        return get_or_create(*args, **kwargs)

    def auto_assign() -> None:
        try:
            auto_assign_check(check, [volunteer], [team])
        except Exception as e:
            failures.append(f"auto-assign: {e}")
        finally:
            connection.close()

    def edit() -> None:
        try:
            assert criteria_read.wait(timeout=60)
            update_check_criteria(
                check,
                [
                    {"id": kept.pk, "label": "Kept", "points": 2, "sort_order": 1},
                    {"id": None, "label": "Added", "points": 3, "sort_order": 2},
                ],
            )
        except Exception as e:
            failures.append(f"rubric edit: {e}")
        finally:
            connection.close()

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(assignments, "get_or_create", get_or_create_once_the_edit_runs)
        threads = [threading.Thread(target=auto_assign), threading.Thread(target=edit)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert failures == []
    assignment = OrangeAssignment.objects.get(orange_check=check)
    assert sorted(assignment.results.values_list("criterion__label", flat=True)) == ["Added", "Kept"]
