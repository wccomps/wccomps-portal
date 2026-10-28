"""The ticket form's box picker presses one box at a time, fills the box's IP, follows a picked service's box, and
keeps the posted box pressed after a failed save."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

BOXES = {"web01": "10.0.1.5", "db01": "10.0.1.6"}
SERVICES = [
    {"value": "db01:MySQL", "label": "db01 - MySQL", "box_name": "db01", "box_ip": "10.0.1.6", "service_name": "MySQL"}
]


def _quotient() -> MagicMock:
    client = MagicMock()
    client.get_infrastructure.return_value = SimpleNamespace(
        boxes=[SimpleNamespace(name=name, ip=ip) for name, ip in BOXES.items()]
    )
    client.get_box_names.return_value = list(BOXES)
    client.get_service_choices.return_value = SERVICES
    return client


@pytest.fixture
def category():
    from ticketing.models import TicketCategory

    return TicketCategory.objects.create(
        pk=90,
        display_name="Box Trouble",
        required_fields=["service_name"],
        optional_fields=["hostname", "ip_address"],
        user_creatable=True,
        sort_order=90,
    )


@pytest.fixture
def page(live_server, pw_browser, blue_team_user, blue_team, category):
    context = create_session_context(pw_browser, live_server, blue_team_user)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    with patch("quotient.client.get_quotient_client", return_value=_quotient()):
        page.goto(f"{live_server.url}/tickets/create/")
        page.select_option("#category", str(category.pk))
        yield page
    context.close()
    assert not errors, errors


def box(page, name):
    return page.locator(f'button[data-value="{name}"]')


def pressed(page):
    return [name for name in BOXES if box(page, name).get_attribute("aria-pressed") == "true"]


def test_one_box_at_a_time_fills_its_ip(page):
    box(page, "web01").click()
    assert pressed(page) == ["web01"]
    assert page.locator("#ip_address").input_value() == "10.0.1.5"

    box(page, "db01").click()
    assert pressed(page) == ["db01"]
    assert page.locator("#ip_address").input_value() == "10.0.1.6"

    box(page, "db01").click()
    assert pressed(page) == []
    assert page.locator("#hostname").input_value() == ""


def test_a_picked_service_presses_its_box(page):
    page.select_option("#service_name", "db01:MySQL")

    assert pressed(page) == ["db01"]
    assert page.locator("#hostname").input_value() == "db01"
    assert page.locator("#ip_address").input_value() == "10.0.1.6"


def test_a_failed_save_keeps_the_box_pressed_and_the_picker_working(page):
    box(page, "web01").click()
    page.evaluate("document.getElementById('ticketForm').noValidate = true")
    with page.expect_navigation():
        page.get_by_role("button", name="Create Ticket").click()  # no title or service: the form comes back

    assert page.locator("#title_error").is_visible()
    assert pressed(page) == ["web01"]
    box(page, "db01").click()
    assert pressed(page) == ["db01"]
    assert page.locator("#ip_address").input_value() == "10.0.1.6"
