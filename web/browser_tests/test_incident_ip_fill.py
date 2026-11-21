"""The incident form fills Affected IP from the first picked box with an IP, keeps an IP the team typed, and still
follows the boxes on the form a failed submit sends back."""

import pytest
from scoring.models import QuotientMetadataCache

from team.models import Team

from .conftest import create_session_context

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

BOXES = [
    {"name": "web", "ip": "192.0.2.20", "services": [{"name": "HTTP", "display_name": "HTTP"}]},
    {"name": "db", "ip": "2001:db8::30", "services": [{"name": "MySQL", "display_name": "MySQL"}]},
    {"name": "router", "ip": "", "services": []},
    {"name": "template", "ip": "10.200.1_.42", "services": []},
    {"name": "hostname", "ip": "web.example.test", "services": []},
    {"name": "partial", "ip": ".42", "services": []},
    {"name": "duplicate", "ip": "192.0.2.41", "services": []},
    {"name": "duplicate", "ip": "192.0.2.42", "services": []},
]


@pytest.fixture
def page(live_server, pw_browser, blue_team_user, blue_team, mock_quotient_client):
    QuotientMetadataCache.objects.create(boxes=BOXES)
    context = create_session_context(pw_browser, live_server, blue_team_user)
    page = context.new_page()
    page.goto(f"{live_server.url}/scoring/incident/submit/")
    yield page
    context.close()


def box(page, name):
    return page.locator(f'button[data-value="{name}"]')


def test_a_box_fills_the_ip_and_a_typed_ip_stays(page):
    ip = page.locator("#id_destination_ip")
    box(page, "web").click()
    assert ip.input_value() == "192.0.2.20"

    ip.fill("10.9.9.9")
    box(page, "web").click()
    box(page, "db").click()
    assert ip.input_value() == "10.9.9.9"


def test_after_a_failed_submit_a_box_change_still_replaces_the_filled_ip(page):
    box(page, "web").click()
    page.evaluate("document.getElementById('incidentForm').noValidate = true")
    # nothing else filled in: the server sends the form back
    with page.expect_navigation():
        page.get_by_role("button", name="Submit Report").click()
    ip = page.locator("#id_destination_ip")
    assert ip.input_value() == "192.0.2.20"

    box(page, "web").click()
    box(page, "db").click()
    assert ip.input_value() == "2001:db8::30"


def test_selecting_unscored_router_box_filters_service_dropdown_to_other(page):
    service_options = page.locator("#id_affected_service option")
    assert service_options.all_inner_texts() == ["Select a service...", "HTTP", "MySQL", "Other"]

    box(page, "router").click()
    assert page.locator("#id_destination_ip").input_value() == ""
    assert service_options.all_inner_texts() == ["Select a service...", "Other"]

    box(page, "web").click()
    assert page.locator("#id_destination_ip").input_value() == "192.0.2.20"
    assert service_options.all_inner_texts() == ["Select a service...", "HTTP", "Other"]


def test_only_unambiguous_literal_addresses_fill_the_ip(page):
    ip = page.locator("#id_destination_ip")
    for name in ("template", "hostname", "partial"):
        box(page, name).click()
        assert ip.input_value() == ""
        box(page, name).click()
    box(page, "duplicate").first.click()
    assert ip.input_value() == ""


def test_staff_team_change_clears_only_an_autofill(live_server, pw_browser, white_team_user, blue_team):
    QuotientMetadataCache.objects.create(boxes=BOXES)
    other = Team.objects.create(team_number=7, team_name="Other team", is_active=True)
    with create_session_context(pw_browser, live_server, white_team_user) as context:
        page = context.new_page()
        page.goto(f"{live_server.url}/scoring/incident/submit/")
        team = page.locator("#id_team")
        ip = page.locator("#id_destination_ip")
        team.select_option(str(blue_team.pk))
        box(page, "web").click()
        assert ip.input_value() == "192.0.2.20"
        team.select_option(str(other.pk))
        assert ip.input_value() == ""
        ip.fill("192.0.2.20")
        team.select_option(str(blue_team.pk))
        assert ip.input_value() == "192.0.2.20"


def test_a_real_submit_saves_a_literal_ip(page):
    box(page, "db").click()
    page.locator("#id_source_ip").fill("192.0.2.90")
    page.locator("#id_attack_detected_at").fill("2026-11-21T12:00")
    page.locator("#id_attack_description").fill("Unexpected login on the database host.")
    page.get_by_role("button", name="Submit Report").click()
    assert page.get_by_text("2001:db8::30", exact=True).is_visible()
