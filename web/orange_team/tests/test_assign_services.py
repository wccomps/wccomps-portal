from collections.abc import Callable

import pytest
from django.contrib.auth.models import User

from orange_team.models import OrangeAssignment, OrangeCheck
from orange_team.services import assign_all_checks, auto_assign_check, rebalance_unstarted
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


def test_rotation_shifts_across_checks(users: list[User], teams: list[Team]) -> None:
    c0 = OrangeCheck.objects.create(title="c0", description="d")
    c1 = OrangeCheck.objects.create(title="c1", description="d")
    auto_assign_check(c0, users, teams, rotation_offset=0)
    auto_assign_check(c1, users, teams, rotation_offset=1)
    team1 = teams[0]
    u0 = OrangeAssignment.objects.get(orange_check=c0, team=team1).user_id
    u1 = OrangeAssignment.objects.get(orange_check=c1, team=team1).user_id
    assert u0 != u1


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


def test_rebalance_moves_pending_off_absent(users: list[User], teams: list[Team]) -> None:
    check = OrangeCheck.objects.create(title="C", description="d")
    auto_assign_check(check, users, teams)
    remaining = [users[0], users[1]]
    rebalance_unstarted(check, remaining)
    assigned = set(OrangeAssignment.objects.filter(orange_check=check).values_list("user_id", flat=True))
    assert users[2].id not in assigned


def test_assign_all_rotates_per_check(users: list[User], teams: list[Team]) -> None:
    checks = [OrangeCheck.objects.create(title=f"c{i}", description="d") for i in range(3)]
    total = assign_all_checks(checks, users, teams)
    assert total == 27
