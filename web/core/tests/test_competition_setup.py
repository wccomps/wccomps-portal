"""The Competition setup page: the schedule, start message, team access, scoring engine and readiness check."""

import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from scoring.models import QuotientMetadataCache

from core.models import CompetitionConfig

pytestmark = pytest.mark.django_db


def _setup_page(client):
    response = client.get(reverse("admin_competition_setup"))
    assert response.status_code == 200
    return response.content.decode()


def _flat(html):
    return re.sub(r"\s+<", "<", re.sub(r">\s+", ">", html))


@pytest.mark.parametrize("user_fixture", ["gold_team_user", "admin_user"])
def test_admin_and_gold_team_get_the_page(user_fixture, request):
    client = Client()
    client.force_login(request.getfixturevalue(user_fixture))

    assert client.get(reverse("admin_competition_setup")).status_code == 200


@pytest.mark.parametrize(
    "user_fixture",
    ["blue_team_user", "red_team_user", "white_team_user", "orange_team_user", "ticketing_admin_user"],
)
def test_other_roles_are_refused(user_fixture, request):
    client = Client()
    client.force_login(request.getfixturevalue(user_fixture))

    assert client.get(reverse("admin_competition_setup")).status_code == 302


def test_anonymous_users_are_sent_to_sign_in(unauthenticated_client):
    assert unauthenticated_client.get(reverse("admin_competition_setup")).status_code == 302


def test_the_page_has_each_moved_section_in_order_and_links_back(admin_client):
    page = _flat(_setup_page(admin_client))

    headings = re.findall(r"<h2>([^<]+)</h2>", page)
    assert headings == ["Schedule", "Start message", "Team access", "Scoring engine (Quotient)", "Readiness check"]
    # The breadcrumb is the only way back
    assert re.search(r'class="breadcrumbs"><a href="' + reverse("admin_competition") + '">Competition</a>', page)
    assert "Back to Competition" not in page


def test_the_page_renders_each_moved_form(admin_client):
    page = _setup_page(admin_client)

    for form in (
        '@submit.prevent="setSchedule"',
        'id="start_datetime"',
        'id="end_datetime"',
        '@submit.prevent="setStartMessage"',
        'id="start_message"',
        '@submit.prevent="setMaxMembers"',
        'id="max_members"',
        '@submit.prevent="addApp"',
        'id="new_app"',
        '@click="syncQuotient"',
        '@click="runReadinessChecks"',
    ):
        assert form in page, form
    # The forms post to the competition page's action endpoint
    assert f"'{reverse('admin_competition_action')}'" in page


def test_the_page_shows_what_is_set_now(admin_client):
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "max_team_members": 7,
            "controlled_applications": ["netbird", "scoring"],
            "competition_start_time": datetime(2027, 10, 31, 16, 0, tzinfo=UTC),
            "start_message": "</script><b>x</b>",
        },
    )
    QuotientMetadataCache.objects.create(boxes=[{"name": "web"}])

    page = _setup_page(admin_client)

    assert "maxMembers: 7," in page
    assert "'netbird', 'scoring'" in page
    assert 'data-iso="2027-10-31T16:00:00+00:00"' in page
    assert '<script id="start-message-data" type="application/json">' in page
    assert "</script><b>x</b>" not in page
    assert "Last synced" in page


def test_the_current_settings_show_in_detail_grids_and_the_small_forms_like_the_teams_page(admin_client):
    QuotientMetadataCache.objects.create(boxes=[{"name": "web"}], team_count=3)

    page = _flat(_setup_page(admin_client))

    for label, value in (("Starts", "Not set"), ("Stops", "Not set"), ("Teams", "3"), ("Boxes", "1")):
        assert re.search(rf'<dl\s+class="detail-list">.*?<dt>{label}</dt><dd>{value}</dd>', page, re.S), label
    assert re.search(r"<dt>Last synced</dt><dd>\d{4}-\d{2}-\d{2} ", page)
    # Labelled inputs beside their button, as the Teams page's bulk actions
    assert re.search(
        r'class="filter-field"[^>]*><label for="max_members" class="filter-field-label">People per team', page
    )
    assert re.search(r'class="filter-field"[^>]*><label for="new_app" class="filter-field-label">Add an app', page)
    # The apps are a table with a Remove button per row, like a team's linked members
    assert re.search(r'<table id="apps_list"\s+aria-label="Competition apps"', page)
    # The visible label names the add field; no aria-label overrides it
    assert 'aria-label="Authentik application slug"' not in page


def test_the_page_says_when_quotient_was_never_synced(admin_client):
    assert "Not synced yet." in _setup_page(admin_client)


def test_the_app_list_is_fetched_after_the_page(admin_client):
    with patch("core.admin_views.competition.AuthentikManager") as manager:
        manager.return_value.list_applications.return_value = ["netbird"]
        page = _setup_page(admin_client)
        manager.return_value.list_applications.assert_not_called()

    assert f"'{reverse('admin_competition_apps')}'" in page


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("set_schedule", {"start_datetime": "2027-10-31T09:00", "start_timezone": "America/Los_Angeles"}),
        ("set_start_message", {"start_message": "Default login"}),
        ("set_max_members", {"max_members": "8"}),
    ],
)
def test_each_moved_form_still_saves_through_its_action(admin_client, action, data):
    response = admin_client.post(reverse("admin_competition_action"), {"action": action, **data})

    assert response.status_code == 200
    assert response.json()["success"]


def test_the_competition_page_reaches_the_setup_page_and_the_nav_names_scoring_setup(admin_client):
    page = admin_client.get(reverse("admin_competition")).content.decode()
    setup = _setup_page(admin_client)

    assert f'href="{reverse("admin_competition_setup")}"' in page
    for html in (page, setup):
        assert ">Scoring setup<" in _flat(html)
        assert ">Configuration<" not in _flat(html)
    # The setup page belongs to the Competition item of the ops navigation
    assert re.search(r'nav-item--current[^>]*aria-current="page">Competition<', _flat(setup))


def test_every_setup_section_the_links_name_is_on_the_page(admin_client):
    from core.admin_views.places import SetupSection

    page = _setup_page(admin_client)

    for section in SetupSection:
        assert f'id="{section}"' in page, section


def test_the_readiness_check_sends_missing_apps_to_the_setup_page():
    from core.admin_views.readiness import _check_blueteam_bindings

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"controlled_applications": []})

    severity, _, action = _check_blueteam_bindings()

    assert severity == "warn"
    assert action is not None and action["url"] == reverse("admin_competition_setup") + "#team-access"


def test_the_empty_leaderboard_sends_admins_to_the_scoring_engine_section(admin_client):
    page = admin_client.get(reverse("leaderboard_page")).content.decode()

    assert f'href="{reverse("admin_competition_setup")}#scoring-engine"' in page


def test_the_scoring_setup_page_is_named_like_its_nav_item(admin_client):
    page = _flat(admin_client.get(reverse("scoring:scoring_config")).content.decode())

    assert ">Scoring setup<" in page
    assert "Scoring Configuration" not in page


def test_readiness_failures_link_the_setup_section_that_fixes_them():
    from unittest.mock import patch

    from core.admin_views import readiness
    from core.admin_views.places import SetupSection, setup_url

    CompetitionConfig.objects.update_or_create(pk=1, defaults={"start_message": ""})
    assert readiness._check_start_message()[2] == {
        "type": "link",
        "url": setup_url(SetupSection.START_MESSAGE),
        "label": "Set Start Message",
    }
    with patch("quotient.client.QuotientClient") as client:
        client.return_value.get_infrastructure.return_value = None
        severity, _, action = readiness._check_quotient_api()
    assert severity == "fail"
    assert action is not None and action["url"] == setup_url(SetupSection.SCORING_ENGINE)
