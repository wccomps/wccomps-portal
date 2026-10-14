"""Shared pytest fixtures for all web tests."""

import os
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client

if TYPE_CHECKING:
    from registration.models import Event


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast if the test database is not reachable."""
    host = os.environ.get("DB_HOST", "localhost")
    port = int(os.environ.get("DB_PORT", "5433"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect((host, port))
    except OSError as err:
        raise pytest.UsageError(
            f"Test database not reachable at {host}:{port}. Run: docker compose -f docker-compose.test.yml up -d --wait"
        ) from err
    finally:
        sock.close()


from core.models import UserGroups
from team.models import DiscordLink


@pytest.fixture
def create_user_with_groups(db: Any) -> Callable[..., User]:
    """Factory fixture to create users with Authentik groups and DiscordLink."""
    _discord_id_counter = [1000000000]

    def _create(username: str, groups: list[str]) -> User:
        user = User.objects.create_user(username=username, password="testpass123")
        UserGroups.objects.create(
            user=user,
            authentik_id=f"{username}-uid",
            groups=groups,
        )
        # Every test user gets a DiscordLink (bot-side permission checks look users up through it)
        DiscordLink.objects.create(
            user=user,
            discord_id=_discord_id_counter[0],
            discord_username=username,
            is_active=True,
        )
        _discord_id_counter[0] += 1
        return user

    return _create


@pytest.fixture
def unauthenticated_client() -> Client:
    return Client()


@pytest.fixture
def blue_team_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("blueteam01", ["WCComps_BlueTeam01"])


@pytest.fixture
def blue_team_02_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("blueteam02", ["WCComps_BlueTeam02"])


@pytest.fixture
def red_team_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("redteam", ["WCComps_RedTeam"])


@pytest.fixture
def gold_team_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("goldteam", ["WCComps_GoldTeam"])


@pytest.fixture
def white_team_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("whiteteam", ["WCComps_WhiteTeam"])


@pytest.fixture
def orange_team_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("orangeteam", ["WCComps_OrangeTeam"])


@pytest.fixture
def ticketing_support_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("support", ["WCComps_Ticketing_Support"])


@pytest.fixture
def ticketing_admin_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("ticketing_admin", ["WCComps_Ticketing_Admin"])


@pytest.fixture
def admin_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("admin", ["WCComps_Discord_Admin"])


@pytest.fixture
def default_category(db):
    """Get the seeded 'Other / General Issue' category (pk=6)."""
    from ticketing.models import TicketCategory

    return TicketCategory.objects.get(pk=6)


@pytest.fixture
def box_reset_category(db):
    """Get the seeded 'Box Reset / Scrub' category (pk=2)."""
    from ticketing.models import TicketCategory

    return TicketCategory.objects.get(pk=2)


@pytest.fixture
def scoring_check_category(db):
    """Get the seeded 'Scoring Service Check' category (pk=3)."""
    from ticketing.models import TicketCategory

    return TicketCategory.objects.get(pk=3)


@pytest.fixture
def phone_consult_category(db):
    """Get the seeded 'Black Team Phone Consultation' category (pk=4)."""
    from ticketing.models import TicketCategory

    return TicketCategory.objects.get(pk=4)


@pytest.fixture(autouse=True)
def ensure_seeded_ticket_categories(request):
    """Restore migration-seeded ticket categories if a transactional test's flush removed them.

    pytest-django runs transactional tests last in a single process, but under xdist worksteal a
    worker can run ordinary tests after a flush, which made category-dependent tests flaky.
    """
    uses_db = request.node.get_closest_marker("django_db") or {"db", "transactional_db"} & set(request.fixturenames)
    if not uses_db:
        return
    from ticketing.tests.seeded_categories import ensure_seeded_categories

    request.getfixturevalue("django_db_setup")
    with request.getfixturevalue("django_db_blocker").unblock():
        ensure_seeded_categories()


@pytest.fixture(autouse=True)
def reset_quotient_client_cache():
    """Reset the QuotientClient LRU cache before each test for proper isolation."""
    from quotient.client import get_quotient_client

    get_quotient_client.cache_clear()
    yield
    get_quotient_client.cache_clear()


@pytest.fixture(autouse=True)
def reset_has_permission_reference():
    """Restore the real has_permission after each test; a module first imported under @patch keeps the mock bound."""
    from core.auth_utils import has_permission as _real

    yield
    import core.auth_utils

    core.auth_utils.has_permission = _real

    import orange_team.views
    import scoring.views.incidents
    import scoring.views.red_team

    import core.admin_views.competition

    orange_team.views.has_permission = _real
    core.admin_views.competition.has_permission = _real
    scoring.views.incidents.has_permission = _real
    scoring.views.red_team.has_permission = _real


@pytest.fixture
def mock_quotient_client():
    """Mock Quotient client to avoid API errors in tests."""
    with patch("quotient.client.QuotientClient") as mock_client:
        instance = MagicMock()
        instance.get_infrastructure.return_value = None
        instance.get_injects.return_value = []
        mock_client.return_value = instance
        yield instance


@pytest.fixture
def current_event(db: Any) -> Event:
    """The event that is open: active, in the active season."""
    from datetime import date

    from registration.models import Event, Season

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational", event_type="invitational", date=date(2026, 10, 3), is_active=True
    )
