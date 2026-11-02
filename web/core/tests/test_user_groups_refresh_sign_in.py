"""A refresh and a sign-in don't write an older Authentik answer over the other's newer one."""

import threading
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone

from core import oauth
from core.models import UserGroups
from core.services.user_groups import refresh_user_groups
from core.tests import test_oauth_flow
from core.tests.test_oauth_flow import oauth_state_session  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

SIGNED_IN = {"sub": "sub-06", "preferred_username": "team06", "groups": ["WCComps_BlueTeam06"]}


def _refresh_reading(answer: list[dict[str, object]], during_the_fetch) -> None:
    def fetch() -> list[dict[str, object]]:
        during_the_fetch()
        return answer

    manager = MagicMock()
    manager.list_all_groups.return_value = [{"pk": "g6", "name": "WCComps_BlueTeam06", "parents": []}]
    manager.list_all_users.side_effect = fetch
    refresh_user_groups(manager)


def _in_another_thread(work) -> None:
    def run() -> None:
        try:
            work()
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()


def test_a_sign_in_during_the_refresh_keeps_what_it_read(oauth_state_session):  # noqa: F811
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[], account_active=False)

    def a_sign_in_commits() -> None:
        _in_another_thread(lambda: test_oauth_flow.TestOAuthCallback._login(client, state, SIGNED_IN))

    _refresh_reading([{"uid": "sub-06", "is_active": False, "groups": []}], a_sign_in_commits)

    row = UserGroups.objects.get(authentik_id="sub-06")
    assert (row.groups, row.account_active) == (["WCComps_BlueTeam06"], True)


def test_a_refresh_during_a_sign_in_keeps_what_it_read(oauth_state_session):  # noqa: F811
    """The account was disabled after this sign-in read Authentik: the sign-in must not mark it enabled again."""
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=["WCComps_BlueTeam06"], account_active=True)
    read_claims = oauth._claims

    def claims_read_before_a_refresh(userinfo):
        _in_another_thread(
            lambda: _refresh_reading([{"uid": "sub-06", "is_active": False, "groups": []}], lambda: None)
        )
        return read_claims(userinfo)

    with patch("core.oauth._claims", side_effect=claims_read_before_a_refresh):
        test_oauth_flow.TestOAuthCallback._login(client, state, SIGNED_IN)

    row = UserGroups.objects.get(authentik_id="sub-06")
    assert (row.groups, row.account_active) == ([], False)


def test_a_refresh_that_changes_nothing_still_outdates_an_older_sign_in(oauth_state_session):  # noqa: F811
    """The refresh read the account disabled after this sign-in read it enabled, and found the row already so."""
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[], account_active=False)
    read_claims = oauth._claims

    def claims_read_before_a_refresh(userinfo):
        _in_another_thread(
            lambda: _refresh_reading([{"uid": "sub-06", "is_active": False, "groups": []}], lambda: None)
        )
        return read_claims(userinfo)

    with patch("core.oauth._claims", side_effect=claims_read_before_a_refresh):
        test_oauth_flow.TestOAuthCallback._login(client, state, SIGNED_IN)

    row = UserGroups.objects.get(authentik_id="sub-06")
    assert (row.groups, row.account_active) == ([], False)


def test_a_sign_in_on_a_pod_whose_clock_runs_ahead_does_not_hold_off_later_refreshes(oauth_state_session):  # noqa: F811
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[], account_active=True)
    ahead = timezone.now() + timedelta(hours=1)

    with patch("django.utils.timezone.now", return_value=ahead):
        test_oauth_flow.TestOAuthCallback._login(client, state, SIGNED_IN)
    _refresh_reading([{"uid": "sub-06", "is_active": False, "groups": []}], lambda: None)

    assert UserGroups.objects.get(authentik_id="sub-06").account_active is False


def test_a_refresh_still_updates_a_row_signed_in_before_it():
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[], account_active=True)

    _refresh_reading([{"uid": "sub-06", "is_active": True, "groups": ["g6"]}], lambda: None)

    assert UserGroups.objects.get(authentik_id="sub-06").groups == ["WCComps_BlueTeam06"]
