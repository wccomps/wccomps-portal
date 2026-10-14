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


def _flat(html):
    """The page with the whitespace around tags removed, so a heading reads <h2>Title</h2>."""
    return re.sub(r"\s+<", "<", re.sub(r">\s+", ">", html))


def test_with_no_event_the_panel_points_at_the_import(client):
    panel = _panel(_page(client))

    assert "No event open" in panel
    assert "No event is open and there is no live data." in panel
    assert f'href="{reverse("school_info_import")}"' in panel
    assert ">No event<" in _flat(panel)


def test_the_title_links_the_guide_and_the_setup_page(client):
    page = _page(client)
    title = page[: page.index('id="event-panel"')]

    assert f'href="{reverse("admin_competition_guide")}"' in title
    assert f'href="{reverse("admin_competition_setup")}"' in title
    assert "Competition setup" in title


def test_an_open_event_names_its_season_schools_and_date_and_points_at_the_packets(client):
    _event(is_active=True)
    _teams(2)

    panel = _flat(_panel(_page(client)))

    assert ">Invitational 1<" in panel
    assert f'Season<a href="{reverse("registration_season_list")}">WCComps 2027</a>' in panel
    assert "2 schools" in panel
    assert "Oct 3, 2027" in panel
    assert ">Stopped<" in panel
    assert f'href="{reverse("packets_list")}"' in panel


def test_while_running_the_panel_offers_stop(client):
    _event(is_active=True)
    _teams(1, score=True)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    panel = _flat(_panel(_page(client)))

    assert ">Running<" in panel
    assert '@click="stopCompetition"' in panel
    assert "When the competition ends, stop it" in panel


def test_played_and_stopped_the_panel_points_at_the_scorecards(client):
    _event(is_active=True)
    _teams(1, score=True)

    panel = _panel(_page(client))

    assert f'href="{reverse("scoring:email_scorecards")}"' in panel
    assert "Teams can't sign in while stopped. Scores stay, so grading goes on." in _page(client)


def test_unrecorded_scores_point_at_close_out(client):
    _teams(1, score=True)

    panel = _panel(_page(client))

    assert "not recorded at any event yet" in panel
    assert f'href="{reverse("admin_competition")}#close-out"' in panel


def test_the_steps_show_one_line_each_without_their_help(client):
    _event(is_active=True)
    _teams(1, score=True)

    panel = _flat(_panel(_page(client)))

    for word in ("Done", "Now", "Next"):
        assert f">{word}<" in panel, word
    assert "White team finishes grading" not in panel


def _sent_packet(event):
    from packets.models import Packet

    Packet.objects.create(
        title="Packet",
        file_data=b"x",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=1,
        status="completed",
        event=event,
    )


def _arrange(stage):
    """Bring the portal to the stage: no event, packets next, start next, running, played, unrecorded, leftovers."""
    if stage == "packets":
        _event(is_active=True)
        _teams(2)
    elif stage == "start":
        _sent_packet(_event(is_active=True))
        _teams(2)
    elif stage in ("running", "played"):
        _event(is_active=True)
        _teams(1, score=True)
        CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": stage == "running"})
    elif stage == "unrecorded":
        Season.objects.create(name="WCComps 2027", year=2027, is_active=True)
        _teams(1, score=True)
    elif stage == "leftovers":
        from ticketing.models import Ticket

        team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
        Ticket.objects.create(ticket_number="T001-001", team=team, title="Left")


SECTION_HEADINGS = {
    "readiness": "<h2>Readiness check</h2>",
    "live": "<h2>Right now</h2>",
    "close-out": "<h2>Before you close out</h2>",
    "leftovers": "<h2>Leftover data</h2>",
}


@pytest.mark.parametrize(
    ("stage", "section"),
    [
        ("idle", None),
        ("packets", None),
        ("start", "readiness"),
        ("running", "live"),
        ("played", "close-out"),
        ("unrecorded", "close-out"),
        ("leftovers", "leftovers"),
    ],
)
def test_the_page_shows_only_the_next_steps_section(client, stage, section):
    _arrange(stage)

    page = _flat(_page(client))

    shown = [key for key, heading in SECTION_HEADINGS.items() if heading in page and f'id="{key}"' in page]
    assert shown == ([section] if section else [])
    # Start and Stop, and the advanced tools, are always there
    assert '@click="startCompetition"' in page and '@click="stopCompetition"' in page
    assert "<summary>Advanced: reset team passwords, Danger Zone</summary>" in page


def test_the_competition_status_table_and_the_setup_forms_are_gone(client):
    page = _page(client)

    assert "Competition Status" not in page
    for moved in ('id="start_datetime"', 'id="start_message"', 'id="max_members"', "syncQuotient", "addApp"):
        assert moved not in page, moved


def test_while_running_the_page_shows_the_live_numbers(client):
    from scoring.models import QuotientMetadataCache

    from core.models import AuditLog
    from ticketing.models import Ticket

    _arrange("running")
    team = Team.objects.get(team_number=1)
    for number, status in enumerate(("open", "claimed", "resolved", "cancelled"), 1):
        Ticket.objects.create(ticket_number=f"T001-00{number}", team=team, title="t", status=status)
    AuditLog.objects.create(
        action="competition_started",
        admin_user="gold",
        target_entity="competition_config",
        target_id=1,
        details={"accounts_ok": 33, "accounts_failed": 1},
    )
    QuotientMetadataCache.objects.create()

    live = _flat(_page(client))
    live = live[live.index('id="live"') :]

    assert ">2<" in live
    assert ">33 of 34<" in live
    assert "Synced" in live and "ago" in live
    for url in (reverse("ticket_list"), reverse("admin_teams"), reverse("leaderboard_page")):
        assert f'href="{url}"' in live, url


def test_running_with_no_recorded_start_or_sync_says_so(client):
    _arrange("running")

    live = _flat(_page(client))
    live = live[live.index('id="live"') :]

    assert ">0<" in live
    assert "Not recorded" in live
    assert "Never synced" in live


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
        assert "Invitational 1" in _page(client)
        response = client.post(reverse("admin_competition_action"), {"action": "close_out_check"})

    assert response.status_code == 500
    assert response.json() == {"error": CLOSE_OUT_CHECK_FAILED}


def test_the_rarely_used_tools_sit_under_advanced_below_the_normal_flow(client):
    _event(is_active=True)
    _teams(1, score=True)
    page = _page(client)

    advanced = page.index("Advanced")
    assert page.index('id="close-out"') < advanced
    assert page.index(f'href="{reverse("admin_competition_danger")}"', advanced) > advanced
    assert page.index('@submit.prevent="resetPasswords"') > advanced


def _queries(client):
    with CaptureQueriesContext(connection) as queries:
        _page(client)
    return len(queries)


@pytest.mark.parametrize("running", [False, True])
def test_the_page_query_count_does_not_grow_with_the_teams(client, running):
    _event(is_active=True)
    _teams(1, score=True)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": running})
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
