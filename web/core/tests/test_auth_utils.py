"""Tests for auth_utils.py permission functions."""

import re

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import RequestFactory
from hypothesis import given
from hypothesis import strategies as st

from core.auth_utils import (
    PERMISSION_MAP,
    check_groups_for_permission,
    get_authentik_groups,
    get_authentik_id,
    get_permissions_context,
    get_user_team,
    has_permission,
    require_permission,
)
from core.tests.guards import REPO_DIR, fail_with, tracked_files
from team.models import Team

pytestmark = pytest.mark.django_db


class TestGetAuthentikGroups:
    """Tests for get_authentik_groups function."""

    def test_anonymous_user_returns_empty_list(self):
        """Anonymous user should return empty groups list."""
        anon = AnonymousUser()
        groups = get_authentik_groups(anon)
        assert groups == []

    def test_user_without_social_account_returns_empty_list(self):
        """User without SocialAccount should return empty groups."""
        user = User.objects.create_user(username="no_social", password="test")
        groups = get_authentik_groups(user)
        assert groups == []

    def test_user_with_social_account_returns_groups(self, blue_team_user):
        """User with SocialAccount should return their groups."""
        groups = get_authentik_groups(blue_team_user)
        assert "WCComps_BlueTeam01" in groups

    def test_groups_from_userinfo(self, admin_user):
        """Groups should be extracted from userinfo.groups."""
        groups = get_authentik_groups(admin_user)
        assert "WCComps_Discord_Admin" in groups


class TestGetAuthentikId:
    """Tests for get_authentik_id function."""

    def test_user_without_usergroups_returns_none(self):
        """User without UserGroups should return None."""
        user = User.objects.create_user(username="no_usergroups", password="test")
        authentik_id = get_authentik_id(user)
        assert authentik_id is None

    def test_user_with_usergroups_returns_id(self, blue_team_user):
        """User with UserGroups should return their authentik_id."""
        authentik_id = get_authentik_id(blue_team_user)
        assert authentik_id == "blueteam01-uid"

    def test_admin_user_returns_id(self, admin_user):
        """Admin user should return their authentik_id."""
        authentik_id = get_authentik_id(admin_user)
        assert authentik_id == "admin-uid"


class TestHasPermission:
    """Tests for has_permission function."""

    def test_anonymous_user_has_no_permissions(self):
        """Anonymous user should have no permissions."""
        anon = AnonymousUser()
        assert not has_permission(anon, "admin")
        assert not has_permission(anon, "ticketing_support")
        assert not has_permission(anon, "gold_team")

    def test_admin_permission(self, admin_user):
        """User with Discord_Admin group should have admin permission."""
        assert has_permission(admin_user, "admin")

    def test_ticketing_support_permission(self, ticketing_support_user):
        """User with Ticketing_Support group should have ticketing_support permission."""
        assert has_permission(ticketing_support_user, "ticketing_support")

    def test_ticketing_admin_permission(self, ticketing_admin_user):
        """User with Ticketing_Admin group should have ticketing_admin permission."""
        assert has_permission(ticketing_admin_user, "ticketing_admin")

    def test_gold_team_permission(self, gold_team_user):
        """User with GoldTeam group should have gold_team permission."""
        assert has_permission(gold_team_user, "gold_team")

    def test_admin_implies_gold_team(self, admin_user):
        """Admin users should also have gold_team permission."""
        assert has_permission(admin_user, "gold_team")

    def test_blue_team_permission(self, blue_team_user):
        """User with BlueTeam group should have blue_team permission."""
        assert has_permission(blue_team_user, "blue_team")

    def test_white_team_permission(self, white_team_user):
        """User with WhiteTeam group should have white_team permission."""
        assert has_permission(white_team_user, "white_team")

    def test_orange_team_permission(self, orange_team_user):
        """User with OrangeTeam group should have orange_team permission."""
        assert has_permission(orange_team_user, "orange_team")

    def test_red_team_user_lacks_blue_team_permission(self, red_team_user):
        """Red team user should not have blue_team permission."""
        assert not has_permission(red_team_user, "blue_team")

    def test_direct_group_check(self, admin_user):
        """Can check for specific group name directly."""
        assert has_permission(admin_user, "WCComps_Discord_Admin")

    def test_nonexistent_permission(self, blue_team_user):
        """Checking for non-existent permission returns False."""
        assert not has_permission(blue_team_user, "nonexistent_permission")


class TestGetPermissionsContext:
    """Tests for get_permissions_context function."""

    def test_admin_permissions_context(self, admin_user):
        """Admin should have is_admin and is_gold_team in context."""
        ctx = get_permissions_context(admin_user)
        assert ctx["is_admin"] is True
        assert ctx["is_gold_team"] is True

    def test_blue_team_permissions_context(self, blue_team_user):
        """Blue team should not have admin permissions in context."""
        ctx = get_permissions_context(blue_team_user)
        assert ctx["is_admin"] is False
        assert ctx["is_ticketing_admin"] is False
        assert ctx["is_gold_team"] is False

    def test_ticketing_support_permissions_context(self, ticketing_support_user):
        """Ticketing support should have is_ticketing_support in context."""
        ctx = get_permissions_context(ticketing_support_user)
        assert ctx["is_ticketing_support"] is True
        assert ctx["is_ticketing_admin"] is False

    def test_all_permission_flags_present(self, blue_team_user):
        """Context should contain all standard permission flags."""
        ctx = get_permissions_context(blue_team_user)
        expected_keys = [
            "is_admin",
            "is_ticketing_admin",
            "is_ticketing_support",
            "is_gold_team",
            "is_white_team",
            "is_orange_team",
        ]
        for key in expected_keys:
            assert key in ctx


class TestGetUserTeam:
    @pytest.fixture
    def teams(self):
        return [Team.objects.create(team_number=n, team_name=f"Team {n}") for n in (1, 2)]

    def test_blue_team_user_gets_their_team(self, teams, blue_team_user, blue_team_02_user):
        assert get_user_team(blue_team_user) == teams[0]
        assert get_user_team(blue_team_02_user) == teams[1]

    def test_non_team_users_get_none(self, teams, ticketing_support_user, admin_user):
        assert get_user_team(ticketing_support_user) is None
        assert get_user_team(admin_user) is None

    def test_anonymous_user_gets_none(self, teams):
        assert get_user_team(AnonymousUser()) is None


class TestRequirePermissionDecorator:
    """Tests for require_permission decorator."""

    def test_decorator_allows_permitted_user(self, ticketing_admin_user):
        """Decorator should allow user with required permission."""

        @require_permission("ticketing_admin")
        def protected_view(request):
            return HttpResponse("success")

        factory = RequestFactory()
        request = factory.get("/test/")
        request.user = ticketing_admin_user

        response = protected_view(request)
        assert response.status_code == 200
        assert response.content == b"success"

    def test_decorator_refuses_unpermitted_user_with_permission_denied(self, blue_team_user):
        """A signed-in user without the permission gets the 403 (core.errors answers PermissionDenied), saying the
        decorator's message."""
        from django.core.exceptions import PermissionDenied

        @require_permission("ticketing_admin", error_message="Only ticketing admins")
        def protected_view(request):
            return HttpResponse("success")

        request = RequestFactory().get("/test/")
        request.user = blue_team_user
        with pytest.raises(PermissionDenied, match="Only ticketing admins"):
            protected_view(request)

    def test_decorator_sends_anonymous_user_to_sign_in(self):
        """A signed-out visitor is sent to sign in, coming back to the page after."""

        @require_permission("ticketing_admin")
        def protected_view(request):
            return HttpResponse("success")

        request = RequestFactory().get("/test/")
        request.user = AnonymousUser()
        response = protected_view(request)
        assert response.status_code == 302
        assert response.url == "/auth/login/?next=%2Ftest%2F"


class TestCheckGroupsForPermissionProperties:
    """Property-based tests for check_groups_for_permission."""

    @given(groups=st.lists(st.text(min_size=1, max_size=50), max_size=10))
    def test_empty_permission_never_granted_by_random_groups(self, groups: list[str]):
        """Empty permission name should not be granted by random groups."""
        result = check_groups_for_permission(groups, "")
        assert result is False

    @given(permission=st.sampled_from(list(PERMISSION_MAP.keys())))
    def test_empty_groups_denies_mapped_permissions(self, permission: str):
        """Empty groups list should deny all mapped permissions."""
        result = check_groups_for_permission([], permission)
        assert result is False

    @given(
        permission=st.sampled_from(list(PERMISSION_MAP.keys())),
        extra_groups=st.lists(st.text(min_size=1, max_size=30), max_size=5),
    )
    def test_permission_granted_when_required_group_present(self, permission: str, extra_groups: list[str]):
        """Permission should be granted when any required group is present."""
        required_groups = PERMISSION_MAP[permission]
        # Add a required group to the list
        groups = extra_groups + [required_groups[0]]
        result = check_groups_for_permission(groups, permission)
        assert result is True

    @given(team_number=st.integers(min_value=1, max_value=50))
    def test_blue_team_permission_with_valid_team_numbers(self, team_number: int):
        """BlueTeam permission should be granted for valid team group patterns."""
        group = f"WCComps_BlueTeam{team_number:02d}"
        result = check_groups_for_permission([group], "blue_team")
        assert result is True

    @given(team_number=st.integers(min_value=51, max_value=99))
    def test_blue_team_permission_with_high_team_numbers(self, team_number: int):
        """BlueTeam permission should still match high team numbers (pattern-based)."""
        group = f"WCComps_BlueTeam{team_number}"
        result = check_groups_for_permission([group], "blue_team")
        # The pattern just checks startswith, so this should still match
        assert result is True

    @given(groups=st.lists(st.text(min_size=1, max_size=50), max_size=10))
    def test_direct_group_check_works(self, groups: list[str]):
        """Direct group name check should work for any group not in PERMISSION_MAP."""
        from core.auth_utils import PERMISSION_MAP

        for group in groups:
            # Skip names that collide with PERMISSION_MAP keys or blue_team (special-cased)
            if group in PERMISSION_MAP or group == "blue_team":
                continue
            result = check_groups_for_permission(groups, group)
            assert result is True

    @given(
        groups=st.lists(
            st.text(
                alphabet=st.characters(blacklist_categories=("Cs",), min_codepoint=32, max_codepoint=126),
                min_size=1,
                max_size=30,
            ),
            max_size=5,
        )
    )
    def test_no_exception_on_arbitrary_input(self, groups: list[str]):
        """Function should not raise exceptions on arbitrary input."""
        for perm in ["admin", "blue_team", "gold_team", "nonexistent", ""]:
            # Should not raise
            check_groups_for_permission(groups, perm)


# A patch of the permission functions where they're defined is seen by a module that looks them up at each call,
# but a module first imported during the patch binds the mock by name and keeps it for the rest of its worker.
# Sign in a role user (core.tests.roles) instead.
PERMISSION_SOURCE_PATCH = re.compile(r"""patch\(\s*["'](core\.(?:auth_utils|permission_constants)\.\w+)""")
PERMISSION_SOURCE_PATCH_EXCEPTIONS: dict[str, str] = {}


def test_no_test_patches_the_permission_functions_where_they_are_defined() -> None:
    found = {
        f"{path}:{text[: match.start()].count(chr(10)) + 1}": match.group(1)
        for path in tracked_files()
        if path.endswith(".py") and path.rsplit("/", 1)[-1].startswith(("test_", "conftest"))
        for text in [(REPO_DIR / path).read_text(encoding="utf-8")]
        for match in PERMISSION_SOURCE_PATCH.finditer(text)
    }
    fail_with("Tests patching a permission function where it's defined", found, PERMISSION_SOURCE_PATCH_EXCEPTIONS)
