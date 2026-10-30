"""Stored Authentik groups follow Authentik, not just the last login (security review, Medium)."""

from unittest.mock import MagicMock

import pytest
from django.contrib.auth.models import User

from core.models import UserGroups
from core.services.user_groups import GroupRefreshAbortedError, refresh_user_groups

pytestmark = pytest.mark.django_db

# Authentik group hierarchy: Ticketing_Admin inherits from Discord_Admin (the OIDC claim includes ancestors)
GROUPS = [
    {"pk": "g-admin", "name": "WCComps_Discord_Admin", "parents": []},
    {"pk": "g-tadmin", "name": "WCComps_Ticketing_Admin", "parents": ["g-admin"]},
    {"pk": "g-blue", "name": "WCComps_BlueTeam", "parents": []},
    {"pk": "g-gold", "name": "WCComps_GoldTeam", "parents": []},
]


def _authentik_user(uid, groups, is_active=True):
    return {"uid": uid, "username": uid, "is_active": is_active, "groups": groups}


def _manager(users, groups=GROUPS):
    manager = MagicMock()
    manager.list_all_groups.return_value = groups
    manager.list_all_users.return_value = users
    return manager


def _stored(uid, groups):
    user = User.objects.create(username=f"user-{uid}")
    return UserGroups.objects.create(user=user, authentik_id=uid, groups=groups)


def _groups(uid):
    return sorted(UserGroups.objects.get(authentik_id=uid).groups)


def test_removed_group_is_dropped_without_a_new_login():
    _stored("a", ["WCComps_Discord_Admin", "WCComps_GoldTeam"])

    result = refresh_user_groups(_manager([_authentik_user("a", ["g-gold"])]))

    assert _groups("a") == ["WCComps_GoldTeam"]
    assert result.changed == 1


def test_added_group_appears_with_its_parents():
    _stored("a", [])

    refresh_user_groups(_manager([_authentik_user("a", ["g-tadmin"])]))

    assert _groups("a") == ["WCComps_Discord_Admin", "WCComps_Ticketing_Admin"]


def test_deactivated_user_keeps_its_membership_but_no_permissions():
    _stored("a", ["WCComps_BlueTeam"])
    _stored("b", ["WCComps_GoldTeam"])

    refresh_user_groups(_manager([_authentik_user("a", ["g-blue"], is_active=False), _authentik_user("b", ["g-gold"])]))

    disabled = UserGroups.objects.get(authentik_id="a")
    assert (disabled.groups, disabled.account_active, disabled.permission_groups) == (["WCComps_BlueTeam"], False, [])
    assert UserGroups.objects.get(authentik_id="b").permission_groups == ["WCComps_GoldTeam"]


def test_reenabling_an_account_restores_its_permissions():
    UserGroups.objects.filter(pk=_stored("a", ["WCComps_GoldTeam"]).pk).update(account_active=False)

    result = refresh_user_groups(_manager([_authentik_user("a", ["g-gold"])]))

    assert UserGroups.objects.get(authentik_id="a").permission_groups == ["WCComps_GoldTeam"]
    assert result.changed == 1


def test_user_deleted_from_authentik_has_no_groups():
    _stored("gone", ["WCComps_Discord_Admin"])
    _stored("b", ["WCComps_GoldTeam"])

    result = refresh_user_groups(_manager([_authentik_user("b", ["g-gold"])]))

    assert _groups("gone") == []
    assert result.changed == 1


def test_unchanged_rows_are_not_rewritten():
    _stored("a", ["WCComps_GoldTeam"])

    result = refresh_user_groups(_manager([_authentik_user("a", ["g-gold"])]))

    assert result.changed == 0
    assert result.checked == 1


def test_empty_authentik_listing_changes_nothing():
    """An empty or wrong-instance answer must not strip everyone's permissions."""
    _stored("a", ["WCComps_Discord_Admin"])

    with pytest.raises(GroupRefreshAbortedError):
        refresh_user_groups(_manager([]))

    assert _groups("a") == ["WCComps_Discord_Admin"]


@pytest.mark.parametrize("groups", [[], [g for g in GROUPS if g["pk"] != "g-gold"], GROUPS[1:]])
def test_incomplete_group_listing_changes_nothing(groups):
    """Users are listed but their groups (or a parent) are missing, e.g. an empty or short groups page."""
    _stored("a", ["WCComps_Discord_Admin", "WCComps_GoldTeam", "WCComps_Ticketing_Admin"])

    with pytest.raises(GroupRefreshAbortedError, match="missing"):
        refresh_user_groups(_manager([_authentik_user("a", ["g-gold", "g-tadmin"])], groups=groups))

    assert _groups("a") == ["WCComps_Discord_Admin", "WCComps_GoldTeam", "WCComps_Ticketing_Admin"]


def test_mostly_unrecognised_ids_change_nothing():
    """If most users with groups aren't found (e.g. the provider's subject mode changed), stop."""
    for uid in ("a", "b", "c"):
        _stored(uid, ["WCComps_GoldTeam"])

    with pytest.raises(GroupRefreshAbortedError):
        refresh_user_groups(_manager([_authentik_user("a", ["g-gold"]), _authentik_user("other", [])]))

    assert _groups("b") == ["WCComps_GoldTeam"]


def test_listing_errors_propagate_without_changes():
    _stored("a", ["WCComps_Discord_Admin"])
    manager = _manager([])
    manager.list_all_users.side_effect = RuntimeError("Authentik down")

    with pytest.raises(RuntimeError):
        refresh_user_groups(manager)

    assert _groups("a") == ["WCComps_Discord_Admin"]


def test_the_refresh_takes_its_lock_before_reading_authentik():
    """Two refreshes (the bot's pass, a competition start or stop) run one at a time, so no refresh commits over
    another refresh's newer snapshot."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _stored("a", [])
    manager = _manager([_authentik_user("a", ["g-gold"])])
    locked_when_reading: list[bool] = []

    with CaptureQueriesContext(connection) as queries:

        def read_groups():
            locked_when_reading.append(any("pg_advisory_xact_lock" in q["sql"] for q in queries.captured_queries))
            return GROUPS

        manager.list_all_groups.side_effect = read_groups
        refresh_user_groups(manager)

    assert locked_when_reading == [True]


def test_a_refresh_reads_the_state_of_an_account_it_had_never_asked_about():
    UserGroups.objects.filter(pk=_stored("a", ["WCComps_GoldTeam"]).pk).update(account_active=None)

    refresh_user_groups(_manager([_authentik_user("a", ["g-gold"], is_active=False)]))

    assert UserGroups.objects.get(authentik_id="a").account_active is False
