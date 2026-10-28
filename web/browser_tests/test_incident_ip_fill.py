"""The incident form fills Affected IP from the first box picked, keeps an IP the team typed, and still follows the
boxes on the form a failed submit sends back."""

import pytest
from scoring.models import QuotientMetadataCache

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

BOXES = [{"name": "web", "ip": "10.2_.0.20", "services": []}, {"name": "db", "ip": "10.2_.0.30", "services": []}]


@pytest.fixture
def page(live_server, pw_browser, blue_team_user, blue_team, mock_quotient_client):
    QuotientMetadataCache.objects.create(boxes=BOXES)
    context = create_session_context(pw_browser, live_server, blue_team_user)
    page = context.new_page()
    page.goto(f"{live_server.url}/scoring/incident/submit/")
    yield page
    context.close()


def box(page, name):
    return page.locator(f'.box-toggle[data-box="{name}"]')


def test_a_box_fills_the_ip_and_a_typed_ip_stays(page):
    ip = page.locator("#id_destination_ip")
    box(page, "web").click()
    assert ip.input_value() == "10.201.0.20"

    ip.fill("10.9.9.9")
    box(page, "web").click()
    box(page, "db").click()
    assert ip.input_value() == "10.9.9.9"


def test_after_a_failed_submit_a_box_change_still_replaces_the_filled_ip(page):
    box(page, "web").click()
    page.evaluate("document.querySelector('form[method=post]').noValidate = true")
    # nothing else filled in: the server sends the form back
    with page.expect_navigation():
        page.get_by_role("button", name="Submit Report").click()
    ip = page.locator("#id_destination_ip")
    assert ip.input_value() == "10.201.0.20"

    box(page, "web").click()
    box(page, "db").click()
    assert ip.input_value() == "10.201.0.30"
