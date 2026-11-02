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


from core.tests.roles import groups_of, make_user, role_user
from team.models import Team


@pytest.fixture(autouse=True)
def _detached_runs_run_inline_in_a_test_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a TestCase's transaction a detached run's own connection couldn't see the test's rows (and would
    commit past its rollback), so there it runs in the caller's thread."""
    from django.db import connection

    from core import utils

    start = utils._start

    def start_or_run(run: Callable[[], None]) -> None:
        if connection.atomic_blocks and connection.atomic_blocks[0]._from_testcase:
            run()
        else:
            start(run)

    monkeypatch.setattr(utils, "_start", start_or_run)


@pytest.fixture
def create_user_with_groups(db: Any) -> Callable[..., User]:
    """Factory fixture to create users with Authentik groups and DiscordLink (core.tests.roles.make_user)."""
    return make_user


@pytest.fixture
def unauthenticated_client() -> Client:
    return Client()


@pytest.fixture
def blue_team_user(db: Any) -> User:
    """The blue_team role's user, a member of Team 1 (core.tests.roles.role_user)."""
    return role_user("blue_team", username="blueteam01")


@pytest.fixture
def blue_team(blue_team_user: User) -> Team:
    """Team 1, the blue_team_user's team."""
    return Team.objects.get(team_number=1)


@pytest.fixture
def blue_group_member_without_team(db: Any) -> User:
    """A user holding the blue_team role's group whose team doesn't exist (no Team 1)."""
    return make_user("blueteam01-noteam", groups_of("blue_team"))


@pytest.fixture
def blue_team_02_user(create_user_with_groups: Callable[..., User]) -> User:
    return create_user_with_groups("blueteam02", ["WCComps_BlueTeam02"])


@pytest.fixture
def red_team_user(db: Any) -> User:
    return role_user("red_team", username="redteam")


@pytest.fixture
def gold_team_user(db: Any) -> User:
    return role_user("gold_team", username="goldteam")


@pytest.fixture
def white_team_user(db: Any) -> User:
    return role_user("white_team", username="whiteteam")


@pytest.fixture
def orange_team_user(db: Any) -> User:
    return role_user("orange_team", username="orangeteam")


@pytest.fixture
def ticketing_support_user(db: Any) -> User:
    return role_user("ticketing_support", username="support")


@pytest.fixture
def ticketing_admin_user(db: Any) -> User:
    return role_user("ticketing_admin", username="ticketing_admin")


@pytest.fixture
def admin_user(db: Any) -> User:
    return role_user("admin", username="admin")


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
def reset_quotient_client_cache():
    """Reset the QuotientClient LRU cache before each test for proper isolation."""
    from quotient.client import get_quotient_client

    get_quotient_client.cache_clear()
    yield
    get_quotient_client.cache_clear()


@pytest.fixture
def mock_quotient_client():
    """Mock Quotient client to avoid API errors in tests."""
    with patch("quotient.client.QuotientClient") as mock_client:
        instance = MagicMock()
        instance.get_infrastructure.return_value = None
        instance.get_injects.return_value = []
        instance.get_service_choices.return_value = []
        instance.get_box_names.return_value = []
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
