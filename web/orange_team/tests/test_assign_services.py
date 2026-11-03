from collections.abc import Callable

import pytest
from django.contrib.auth.models import User

from orange_team.models import OrangeAssignment, OrangeCheck
from orange_team.services import auto_assign_check, rebalance_unstarted
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def users(create_user_with_groups: Callable[..., User]) -> list[User]:
    return [create_user_with_groups(f"v{i}", ["WCComps_OrangeTeam"]) for i in range(3)]


@pytest.fixture
def teams() -> list[Team]:
    return [Team.objects.create(team_number=i, team_name=f"T{i}") for i in range(1, 10)]


def test_balanced_and_covers_everyone(users: list[User], teams: list[Team]) -> None:
    check = OrangeCheck.objects.create(title="C", description="d", max_points=10)
    count = auto_assign_check(check, users, teams)
    assert count == 9
    per_user = {u.id: OrangeAssignment.objects.filter(orange_check=check, user=u).count() for u in users}
    assert sorted(per_user.values()) == [3, 3, 3]


def test_randomizes_assignments_across_checks(users: list[User], teams: list[Team]) -> None:
    checks = [OrangeCheck.objects.create(title=f"c{i}", description="d") for i in range(6)]
    for check in checks:
        auto_assign_check(check, users, teams)
    mappings = {
        tuple(
            OrangeAssignment.objects.filter(orange_check=c)
            .order_by("team__team_number")
            .values_list("user_id", flat=True)
        )
        for c in checks
    }
    assert len(mappings) > 1
    for check in checks:
        per_user = [OrangeAssignment.objects.filter(orange_check=check, user=u).count() for u in users]
        assert sorted(per_user) == [3, 3, 3]


def test_unstarted_remainders_are_balanced_when_some_teams_are_already_started(
    users: list[User], teams: list[Team]
) -> None:
    check = OrangeCheck.objects.create(title="C", description="d")
    for team in teams[:3]:
        OrangeAssignment.objects.create(orange_check=check, user=users[0], team=team, status="in_progress")
    count = auto_assign_check(check, users, teams)
    assert count == 6
    unstarted_per_user = [
        OrangeAssignment.objects.filter(orange_check=check, user=u, status="pending").count() for u in users
    ]
    assert sorted(unstarted_per_user) == [2, 2, 2]


def test_no_checked_in_users_is_noop(teams: list[Team]) -> None:
    check = OrangeCheck.objects.create(title="C", description="d")
    assert auto_assign_check(check, [], teams) == 0
    assert OrangeAssignment.objects.count() == 0


def test_rerun_preserves_scored_cells(users: list[User], teams: list[Team]) -> None:
    check = OrangeCheck.objects.create(title="C", description="d", max_points=10)
    auto_assign_check(check, users, teams)
    locked = OrangeAssignment.objects.get(orange_check=check, team=teams[0])
    locked.status = "approved"
    locked.user = users[0]
    locked.save()
    auto_assign_check(check, [users[1]], teams)
    locked.refresh_from_db()
    assert locked.user_id == users[0].id
    assert locked.status == "approved"
    pending = OrangeAssignment.objects.get(orange_check=check, team=teams[1])
    assert pending.user_id == users[1].id


def test_rerun_preserves_in_progress_cells(users: list[User], teams: list[Team]) -> None:
    check = OrangeCheck.objects.create(title="C", description="d", max_points=20)
    auto_assign_check(check, users, teams)
    started = OrangeAssignment.objects.get(orange_check=check, team=teams[0])
    started.status = "in_progress"
    started.score = 15
    started.user = users[0]
    started.save()
    auto_assign_check(check, [users[1]], teams)  # re-run with a different roster
    started.refresh_from_db()
    assert started.user_id == users[0].id  # not handed to another volunteer
    assert started.score == 15  # partial score not carried onto someone else


def test_rebalance_moves_pending_off_absent(users: list[User], teams: list[Team]) -> None:
    check = OrangeCheck.objects.create(title="C", description="d")
    auto_assign_check(check, users, teams)
    remaining = [users[0], users[1]]
    rebalance_unstarted(check, remaining)
    assigned = set(OrangeAssignment.objects.filter(orange_check=check).values_list("user_id", flat=True))
    assert users[2].id not in assigned
