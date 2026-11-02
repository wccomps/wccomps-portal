"""During a rolling deploy, pods still on the previous version create UserGroups rows without account_active. Those
rows read null (not yet asked of Authentik): permissions as an enabled account, Discord roles held until a refresh."""

import pytest
from django.contrib.auth.models import User
from django.db import connection

from core.models import UserGroups

pytestmark = pytest.mark.django_db


def test_a_row_written_without_account_active_is_not_yet_asked():
    user = User.objects.create(username="first-login")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core_usergroups (user_id, authentik_id, groups) VALUES (%s, %s, %s)",
            [user.pk, "sub-first", '["WCComps_GoldTeam"]'],
        )

    row = UserGroups.objects.get(user=user)
    assert (row.account_active, row.permission_groups) == (None, ["WCComps_GoldTeam"])
