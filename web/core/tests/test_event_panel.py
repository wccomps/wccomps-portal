"""The competition page's event panel, and the Running an event guide it links to."""

import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from registration.models import Event, Season
from registration.services import set_team_school
from scoring.models import ServiceScore

from core.admin_views.competition import CLOSE_OUT_CHECK_FAILED
from core.admin_views.event_panel import RARE_ACTIONS, STEPS
from core.models import CompetitionConfig
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _page(client):
    response = client.get(reverse("admin_competition"))
    assert response.status_code == 200
    return response.content.decode()


def _panel(page):
    """The panel's HTML: from its opening tag to the steps list's end."""
    start = page.index('id="event-panel"')
    return page[start : page.index("</ol>", start)]


def _event(**fields):
    season = Season.objects.create(name="WCComps 2027", year=2027, is_active=True)
    return Event.objects.create(
        season=season, name="Invitational 1", event_type="invitational", date=date(2027, 10, 3), **fields
    )


def _teams(count, score=False):
    for number in range(1, count + 1):
        team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
        set_team_school(team, f"School {number}", contact_email="c@example.edu")
        if score:
            ServiceScore.objects.create(team=team, service_points=Decimal(10))


def test_with_no_event_the_panel_points_at_the_import(client):
    panel = _panel(_page(client))

    assert "No event open" in panel
    assert "No event is open and there is no live data." in panel
    assert f'href="{reverse("school_info_import")}"' in panel
    assert f'href="{reverse("admin_competition_guide")}"' in panel


def test_an_open_event_names_its_season_and_points_at_the_packets(client):
    _event(is_active=True)
    _teams(2)

    panel = _panel(_page(client))

    assert "Invitational 1, season" in panel
    assert f'<a href="{reverse("registration_season_list")}">WCComps 2027</a>' in panel
    assert "Invitational 1 (WCComps 2027) is open. No team has scores yet." in panel
    assert f'href="{reverse("packets_list")}"' in panel
    assert "2 schools are on the list." in panel


def test_while_running_the_panel_points_at_stop(client):
    _event(is_active=True)
    _teams(1, score=True)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    panel = _panel(_page(client))

    assert "The competition is running." in panel
    assert f'href="{reverse("admin_competition")}#controls"' in panel
    assert 'id="controls"' in _page(client)


def test_played_and_stopped_the_panel_points_at_the_scorecards_and_close_out(client):
    _event(is_active=True)
    _teams(1, score=True)

    page = _page(client)
    panel = _panel(page)

    assert f'href="{reverse("scoring:email_scorecards")}"' in panel
    assert f'href="{reverse("admin_competition")}#close-out"' in panel
    assert 'x-text="closeOutSummary"' in panel
    assert 'id="close-out"' in page


def test_unrecorded_scores_point_at_close_out(client):
    _teams(1, score=True)

    panel = _panel(_page(client))

    assert "not recorded at any event yet" in panel
    assert f'href="{reverse("admin_competition")}#close-out"' in panel


def test_leftovers_point_at_the_danger_zone(client):
    from ticketing.models import Ticket

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    Ticket.objects.create(ticket_number="T001-001", team=team, title="Left")

    panel = _panel(_page(client))

    assert "Clear leftover data" in panel
    assert f'href="{reverse("admin_competition_danger")}"' in panel


def test_a_failing_close_out_check_does_not_break_the_page(client):
    _event(is_active=True)
    _teams(1, score=True)

    with patch("core.admin_views.competition.close_out_warnings", side_effect=RuntimeError("boom")):
        assert "Invitational 1, season" in _page(client)
        response = client.post(reverse("admin_competition_action"), {"action": "close_out_check"})

    assert response.status_code == 500
    assert response.json() == {"error": CLOSE_OUT_CHECK_FAILED}


def test_the_rarely_used_tools_sit_under_advanced_below_the_normal_flow(client):
    page = _page(client)

    advanced = page.index("Advanced")
    assert page.index('id="close-out"') < advanced
    assert page.index(f'href="{reverse("admin_competition_danger")}"', advanced) > advanced
    assert page.index('@submit.prevent="resetPasswords"') > advanced


def _queries(client):
    with CaptureQueriesContext(connection) as queries:
        _page(client)
    return len(queries)


def test_the_page_query_count_does_not_grow_with_the_teams(client):
    _event(is_active=True)
    _teams(1, score=True)
    _page(client)  # The first request also loads the session and caches
    one = _queries(client)

    Team.objects.all().delete()
    _teams(20, score=True)

    assert _queries(client) == one
    assert one <= 40


def test_the_guide_walks_the_panels_steps_in_order_with_their_links(client):
    response = client.get(reverse("admin_competition_guide"))

    assert response.status_code == 200
    page = response.content.decode()
    headings = re.findall(r'<h2 class="mt-20" id="([a-z_]+)">', page)
    assert headings == [step.key for step in STEPS] + [action.value for action, _ in RARE_ACTIONS]
    hrefs = set(re.findall(r'href="([^"]*)"', page))
    for step in STEPS:
        assert step.url in hrefs, step.key
    assert reverse("admin_competition_danger") in hrefs
    assert "\u2014" not in page and "\u2013" not in page


def test_every_guide_link_resolves(client):
    page = client.get(reverse("admin_competition_guide")).content.decode()
    main = page[page.index('id="event-guide"') :]
    for href in set(re.findall(r'href="(/[^"#]*)', main)):
        assert client.get(href).status_code in (200, 302), href


def test_the_guide_is_for_gold_team_and_admins_only(blue_team_user, white_team_user, admin_user):
    for user, allowed in ((blue_team_user, False), (white_team_user, False), (admin_user, True)):
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin_competition_guide"))
        assert (response.status_code == 200) == allowed, user.username


def test_the_guide_is_in_the_ops_navigation(client):
    page = client.get(reverse("admin_competition_guide")).content.decode()
    assert 'aria-current="page"' in page
    with pytest.raises(NoReverseMatch):
        reverse("admin_competition_guide", args=[1])


def test_the_competition_page_uses_words_not_symbol_marks(client):
    page = _page(client)

    for mark in ("\u2713", "\u2717", "\u26a0", "\u2192"):
        assert mark not in page, mark


def test_the_finish_step_says_an_unrecorded_send_is_fine(client):
    _event(is_active=True)
    _teams(1, score=True)

    assert "If you already emailed them, you can close out." in _panel(_page(client))


def test_with_no_active_season_close_out_links_the_seasons_page(client):
    Season.objects.create(name="2025", year=2025)
    _teams(1, score=True)

    page = _page(client)
    close_out = page[page.index('id="close-out"') :]

    assert "No season is active" in close_out
    assert f'href="{reverse("registration_season_list")}"' in close_out
    assert "Registration, Seasons" not in page


def test_the_guide_links_the_seasons_page_and_says_where_the_checks_show(client):
    page = client.get(reverse("admin_competition_guide")).content.decode()

    assert f'href="{reverse("registration_season_list")}"' in page
    assert "If you already emailed them, you can close out." in page
    assert "this step and the next" not in page
