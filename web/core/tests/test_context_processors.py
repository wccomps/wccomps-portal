"""Tests for context processors."""

import inspect
import re
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory

from core.context_processors import NAV_MAPPING, permissions
from core.models import UserGroups
from core.tests.guards import extends_chain, fail_with, page_templates, tags, template_name

pytestmark = pytest.mark.django_db


@pytest.fixture
def request_factory():
    """Create a RequestFactory for generating mock requests."""
    return RequestFactory()


class TestPermissionsContextProcessor:
    """Test the permissions context processor."""

    def test_unauthenticated_user_returns_false_for_all_flags(self, request_factory):
        """Unauthenticated users should get False for all permission flags."""
        from django.contrib.auth.models import AnonymousUser

        request = request_factory.get("/")
        request.user = AnonymousUser()

        context = permissions(request)

        assert context["is_admin"] is False
        assert context["is_ticketing_admin"] is False
        assert context["is_ticketing_support"] is False
        assert context["is_gold_team"] is False
        assert context["is_blue_team"] is False
        assert context["is_red_team"] is False
        assert context["is_white_team"] is False
        assert context["is_orange_team"] is False
        assert context["authentik_username"] == ""

    def test_white_team_user_has_is_white_team_true(self, request_factory):
        """User in WCComps_WhiteTeam group should have is_white_team = True."""
        user = User.objects.create_user(username="whiteteam", password="test")
        UserGroups.objects.create(user=user, authentik_id="white-team-uid", groups=["WCComps_WhiteTeam"])

        request = request_factory.get("/")
        request.user = user

        context = permissions(request)

        assert context["is_white_team"] is True
        assert context["is_orange_team"] is False
        assert context["is_gold_team"] is False
        assert context["is_blue_team"] is False
        assert context["is_red_team"] is False

    def test_orange_team_user_has_is_orange_team_true(self, request_factory):
        """User in WCComps_OrangeTeam group should have is_orange_team = True."""
        user = User.objects.create_user(username="orangeteam", password="test")
        UserGroups.objects.create(user=user, authentik_id="orange-team-uid", groups=["WCComps_OrangeTeam"])

        request = request_factory.get("/")
        request.user = user

        context = permissions(request)

        assert context["is_orange_team"] is True
        assert context["is_white_team"] is False
        assert context["is_gold_team"] is False
        assert context["is_blue_team"] is False
        assert context["is_red_team"] is False

    def test_user_with_multiple_teams(self, request_factory):
        """User in both WhiteTeam and OrangeTeam should have both flags True."""
        user = User.objects.create_user(username="multipleams", password="test")
        UserGroups.objects.create(
            user=user, authentik_id="multiple-teams-uid", groups=["WCComps_WhiteTeam", "WCComps_OrangeTeam"]
        )

        request = request_factory.get("/")
        request.user = user

        context = permissions(request)

        assert context["is_white_team"] is True
        assert context["is_orange_team"] is True

    def test_gold_team_user_has_white_and_orange_team(self, request_factory):
        """Gold team user should have is_white_team and is_orange_team True (hierarchy)."""
        user = User.objects.create_user(username="goldteam", password="test")
        UserGroups.objects.create(user=user, authentik_id="gold-team-uid", groups=["WCComps_GoldTeam"])

        request = request_factory.get("/")
        request.user = user

        context = permissions(request)

        assert context["is_white_team"] is True
        assert context["is_orange_team"] is True
        assert context["is_gold_team"] is True

    def test_admin_user_has_all_team_flags(self, request_factory):
        """Admin user should have all team permission flags True."""
        user = User.objects.create_user(username="admin", password="test")
        UserGroups.objects.create(user=user, authentik_id="admin-uid", groups=["WCComps_Discord_Admin"])

        request = request_factory.get("/")
        request.user = user

        context = permissions(request)

        assert context["is_orange_team"] is True
        assert context["is_red_team"] is True
        assert context["is_white_team"] is True
        assert context["is_admin"] is True

    def test_user_without_usergroups_returns_false(self, request_factory):
        """User without UserGroups should have all team flags False."""
        user = User.objects.create_user(username="nosocial", password="test")

        request = request_factory.get("/")
        request.user = user

        context = permissions(request)

        assert context["is_white_team"] is False
        assert context["is_orange_team"] is False
        assert context["is_gold_team"] is False
        assert context["is_blue_team"] is False
        assert context["is_red_team"] is False


RENDER_RE = re.compile(r"""render\s*\(\s*request\s*,\s*["']([^"']+)["']""")
NAV_MAPPING_EXCEPTIONS: dict[str, str] = {}


def _url_templates() -> dict[str, set[str]]:
    """Each URL name and the templates its view renders (each render(request, "...") in the view's source)."""
    from django.urls import URLPattern, URLResolver, get_resolver

    found: dict[str, set[str]] = {}

    def extract(patterns: list[URLPattern | URLResolver]) -> None:
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                extract(pattern.url_patterns)
            elif pattern.name and inspect.isfunction(view := inspect.unwrap(pattern.callback)):
                found.setdefault(pattern.name, set()).update(RENDER_RE.findall(inspect.getsource(view)))

    extract(get_resolver().url_patterns)
    return found


def _below_the_chrome(path: Path) -> tuple[str, ...]:
    """The template and those it extends up to the chrome, admin/base_site.html. Past it, the chrome's
    {% extends "admin/base.html" %} loads the ops section's base too, whose content block (and sub-nav) a page under
    any other base replaces."""
    chain = extends_chain(path)
    return chain[: chain.index("admin/base_site.html")] if "admin/base_site.html" in chain else chain


def test_subnav_pages_have_nav_mapping() -> None:
    """A page under a section base with a sub-nav (<c-nav>) needs a NAV_MAPPING entry to highlight its item."""
    subnav_bases = {template_name(p) for p in page_templates() if any(tag.name == "c-nav" for tag in tags(p))}
    with_subnav = {template_name(p) for p in page_templates() if subnav_bases & set(_below_the_chrome(p))}
    missing = {
        f"{url_name} -> {template}": ""
        for url_name, templates in _url_templates().items()
        for template in sorted(templates & with_subnav)
        if url_name not in NAV_MAPPING
    }
    fail_with("URLs with a sub-nav missing from NAV_MAPPING", missing, NAV_MAPPING_EXCEPTIONS)
