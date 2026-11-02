"""The assign box suggests only staff whose Authentik account is enabled."""

import pytest
from django.contrib.auth.models import User

from core.models import UserGroups
from ticketing.views.detail import _ticketing_staff_usernames

pytestmark = pytest.mark.django_db


def test_a_disabled_staff_account_is_not_suggested():
    for name, active in (("support-on", True), ("support-off", False)):
        user = User.objects.create(username=name)
        UserGroups.objects.create(
            user=user, authentik_id=f"sub-{name}", groups=["WCComps_Ticketing_Support"], account_active=active
        )

    assert _ticketing_staff_usernames() == ["support-on"]
