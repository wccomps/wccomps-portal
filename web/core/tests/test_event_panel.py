"""The competition page's Event and Steps sections."""

import re
from contextlib import ExitStack
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
from core.admin_views.places import PLACES, STEPS, action_url
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


def _event_section(page):
    """The Event section's HTML: from its opening tag to the Steps section."""
    start = page.index('id="event-panel"')
    return page[start : page.index('id="steps"', start)]


def _steps(page):
    """The Steps table's rows as (step cell, status cell, where cell), whitespace around tags removed."""
    start = page.index('id="steps"')
    body = _flat(page[page.index("<tbody>", start) : page.index("</table>", start)])
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
    return [tuple(re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)) for row in rows]


def _row(panel, label):
    """The value of the Event module's detail row with the label."""
    return re.search(rf"<dt>{label}</dt><dd>(.*?)</dd>", _flat(panel), re.S).group(1)


def _action(panel):
    """The href of the link in the Event module's button row, or None if it offers no link."""
    match = re.search(r'<div[^>]*class="submit-row[^"]*"[^>]*><a href="([^"]*)"', _flat(panel))
    return match and match.group(1)


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


def test_with_no_event_the_event_section_points_at_the_import(client):
    panel = _event_section(_page(client))

    assert _row(panel, "Event") == "No event open"
    assert "No event is open and there is no live data." in _row(panel, "Status")
    assert _action(panel) == reverse("school_info_import")
    assert ">No event</span>" in _row(panel, "Status")


def test_the_toolbar_links_the_setup_page(client):
    page = _flat(_page(client))
    toolbar = page[page.index('<div class="toolbar">') : page.index('id="event-panel"')]

    assert re.search(f'<a href="{reverse("admin_competition_setup")}"[^>]*>Competition setup</a>', toolbar)


def test_an_open_event_names_its_season_schools_and_date_and_points_at_the_packets(client):
    _event(is_active=True)
    _teams(2)

    panel = _event_section(_page(client))

    assert _row(panel, "Event") == "Invitational 1"
    assert _row(panel, "Season") == f'<a href="{reverse("registration_season_list")}">WCComps 2027</a>'
    assert _row(panel, "Schools") == "2"
    assert _row(panel, "Date") == "Oct 3, 2027"
    assert ">Not started<" in _row(panel, "Status")
    assert _action(panel) == reverse("packets_list")


def test_while_running_the_event_section_says_to_stop_and_leaves_the_button_to_the_controls(client):
    _event(is_active=True)
    _teams(1, score=True)
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": True})

    panel = _flat(_event_section(_page(client)))

    assert ">Running<" in panel
    assert "When the competition ends, stop it" in panel
    assert "Stop Competition</button>" not in panel


def test_played_and_stopped_the_event_section_points_at_the_scorecards(client):
    _event(is_active=True)
    _teams(1, score=True)

    panel = _event_section(_page(client))

    assert _action(panel) == reverse("scoring:email_scorecards")
    assert "Teams can't sign in while stopped. Scores stay, so grading goes on." in _page(client)


def test_unrecorded_scores_point_at_the_scorecards_then_close_out_on_this_page(client):
    _teams(1, score=True)

    page = _page(client)

    assert "not recorded at any event yet" in _row(_event_section(page), "Status")
    assert _action(_event_section(page)) == reverse("scoring:email_scorecards")
    assert _steps(page)[-1][2] == "On this page"


def test_the_steps_show_one_line_each_without_their_help(client):
    _event(is_active=True)
    _teams(1, score=True)

    steps = _steps(_page(client))

    statuses = [re.search(r">([^<]+)</span>", status).group(1) for _, status, _ in steps]
    assert statuses == ["Done", "Later", "Done", "Done", "Now", "Next"]
    assert all("White team finishes grading" not in step for step, _, _ in steps)


@pytest.mark.parametrize("stage", ["idle", "start", "played"])
def test_no_step_name_is_a_link_and_where_links_only_other_pages(client, stage):
    _arrange(stage)

    steps = _steps(_page(client))

    assert [step.split("<")[0] for step, _, _ in steps] == [
        f"{number}. {PLACES[action].heading}" for number, action in enumerate(STEPS, 1)
    ]
    assert all("<a " not in step for step, _, _ in steps)
    wheres = [where for _, _, where in steps]
    assert wheres[0] == f'<a href="{reverse("school_info_import")}">Import School Information from CSV</a>'
    assert wheres[1] == f'<a href="{reverse("packets_list")}">Packets</a>'
    assert wheres[2:4] == ["On this page", "On this page"]
    assert wheres[4] == f'<a href="{reverse("scoring:email_scorecards")}">Email All Scorecards</a>'
    assert wheres[5] == "On this page"


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
    # One Start and one Stop, both in Competition Controls; the one the state already matches is disabled
    assert page.count(">Start Competition</button>") == 1
    assert page.count(">Stop Competition</button>") == 1
    assert page.count('@click="startCompetition"') == (0 if stage == "running" else 1)
    assert page.count('@click="stopCompetition"') == (1 if stage == "running" else 0)
    # Close Out is the section's button only: no next-step link to it, no step link
    assert page.count('@click="closeOutEvent"') == (1 if section == "close-out" else 0)
    for label in ("Close out the event", "Check readiness and start"):
        assert f">{label}</a>" not in page, label
    assert 'id="advanced"' in page and "Advanced: reset team passwords" in page
    assert page.count('@click="cancelEvent"') == (1 if stage in ("packets", "start") else 0)
    assert page.count('@click="clearLeftovers"') == (1 if stage == "leftovers" else 0)


ALL_STAGES = ["idle", "packets", "start", "running", "played", "unrecorded", "leftovers"]


def _ids(html):
    return set(re.findall(r'\bid="([^"]+)"', html))


@pytest.mark.parametrize("stage", ALL_STAGES)
def test_no_link_on_the_page_lands_on_a_missing_section(client, stage):
    _arrange(stage)
    pages = {
        reverse("admin_competition"): _page(client),
        reverse("admin_competition_setup"): client.get(reverse("admin_competition_setup")).content.decode(),
    }

    for html in pages.values():
        for path, anchor in re.findall(r'href="(/ops/admin/competition/(?:setup/)?)#([^"]+)"', html):
            assert anchor in _ids(pages[path]), f"{path}#{anchor} in stage {stage}"


@pytest.mark.parametrize(
    ("stage", "word"),
    [
        ("idle", "No event"),
        ("packets", "Not started"),
        ("start", "Not started"),
        ("running", "Running"),
        ("played", "Stopped"),
        ("unrecorded", "No event"),
    ],
)
def test_the_status_badge_says_where_the_competition_stands(client, stage, word):
    _arrange(stage)

    assert f">{word}</span>" in _row(_event_section(_page(client)), "Status")


def test_the_event_section_counts_the_schools_and_the_linked_team_members(client, gold_team_user):
    from team.models import DiscordLink

    _event(is_active=True)
    _teams(2)
    DiscordLink.objects.create(
        team=Team.objects.get(team_number=1), discord_id=5, discord_username="b", user=gold_team_user
    )

    panel = _event_section(_page(client))

    assert _row(panel, "Schools") == "2"
    assert _row(panel, "Team members linked") == "1"


def test_before_any_scores_the_next_step_says_how_to_call_the_event_off(client):
    _arrange("packets")

    assert "No team has scores yet. To call the event off, use Cancel event" in _row(
        _event_section(_page(client)), "Next step"
    )


def test_the_competition_status_table_and_the_setup_forms_are_gone(client):
    page = _page(client)

    assert "Competition Status" not in page
    for moved in ('id="start_datetime"', 'id="start_message"', 'id="max_members"', "syncQuotient", "addApp"):
        assert moved not in page, moved


def test_while_running_the_page_shows_the_live_numbers(client):
    from scoring.models import QuotientMetadataCache

    from ticketing.models import Ticket

    _arrange("running")
    team = Team.objects.get(team_number=1)
    for number, status in enumerate(("open", "claimed", "resolved", "cancelled"), 1):
        Ticket.objects.create(ticket_number=f"T001-00{number}", team=team, title="t", status=status)
    CompetitionConfig.objects.filter(pk=1).update(last_start_accounts_ok=33, last_start_accounts_tried=34)
    QuotientMetadataCache.objects.create()

    live = _flat(_page(client))
    live = live[live.index('id="live"') :]

    assert "Open or claimed tickets" in live and ">2<" in live
    assert ">33 of 34<" in live
    assert "ago" in live
    for url in (reverse("ticket_list"), reverse("admin_teams"), reverse("admin_competition_setup") + "#scoring-engine"):
        assert f'href="{url}"' in live, url


def test_running_with_no_recorded_start_or_sync_says_so(client):
    _arrange("running")

    live = _flat(_page(client))
    live = live[live.index('id="live"') :]

    assert ">0<" in live
    assert "Not recorded" in live
    assert "Never synced" in live


def test_leftovers_open_advanced_where_clear_leftover_data_is(client):
    from ticketing.models import Ticket

    team = Team.objects.create(team_number=1, team_name="Team 01", is_active=True)
    Ticket.objects.create(ticket_number="T001-001", team=team, title="Left")

    page = _page(client)
    panel = _flat(_event_section(page))

    assert "Clear leftover data under Advanced on the Competition page" in panel
    assert _action(panel) is None
    assert "under Advanced below" in _flat(page[page.index('id="leftovers"') :])
    # The status sentence shows once, in the Event section
    assert _flat(page).count("there is live data with no scores") == 1
    assert "Live data no event recorded is still here" not in page
    advanced = page[page.index('id="advanced"') :]
    assert 'data-collapsed="false"' in advanced[: advanced.index(">")]
    assert page.index('@click="clearLeftovers"') > page.index('id="advanced"')
    assert "1 Ticket" in _flat(advanced)


def test_advanced_stays_collapsed_outside_the_leftovers_stage(client):
    _event(is_active=True)
    _teams(1)

    page = _page(client)
    advanced = page[page.index('id="advanced"') :]

    assert 'data-collapsed="true"' in advanced[: advanced.index(">")]


def test_the_danger_page_is_gone(client):
    assert client.get("/ops/admin/competition/danger/").status_code == 404
    with pytest.raises(NoReverseMatch):
        reverse("admin_competition_danger")


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

    advanced = page.index('id="advanced"')
    assert page.index('id="close-out"') < advanced
    assert page.index('@submit.prevent="resetPasswords"') > advanced


def test_cancel_sits_under_advanced_below_the_normal_flow(client):
    _event(is_active=True)
    _teams(1)
    page = _page(client)

    assert page.index('@click="cancelEvent"') > page.index('id="advanced"')


def _queries(client):
    with CaptureQueriesContext(connection) as queries:
        _page(client)
    return len(queries)


def _leftover_tickets(count):
    from ticketing.models import Ticket

    for number in range(1, count + 1):
        team = Team.objects.create(team_number=number, team_name=f"Team {number:02d}", is_active=True)
        Ticket.objects.create(ticket_number=f"T{number:03d}-001", team=team, title="Left")


@pytest.mark.parametrize("stage", ["played", "running", "leftovers"])
def test_the_page_query_count_does_not_grow_with_the_teams(client, stage):
    if stage == "leftovers":
        _leftover_tickets(1)
    else:
        _event(is_active=True)
        _teams(1, score=True)
        CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": stage == "running"})
    _page(client)  # The first request also loads the session and caches
    one = _queries(client)

    Team.objects.all().delete()
    if stage == "leftovers":
        _leftover_tickets(20)
    else:
        _teams(20, score=True)

    assert _queries(client) == one
    assert one <= 40


@pytest.mark.parametrize("action", [action for action, place in PLACES.items() if place.elsewhere])
def test_a_page_a_step_links_names_itself_with_the_title_the_step_shows(admin_client, action):
    """The Where link and the linked page's header read one title."""
    place = PLACES[action]
    _teams(1, score=True)  # Email Scorecards sends you to the leaderboard with no scores

    response = admin_client.get(action_url(action))

    assert response.status_code == 200
    page = _flat(response.content.decode())

    parent = r"(<a [^>]*>[^<]*</a><span[^>]*>[^<]*</span>)?"
    assert re.search(rf'class="breadcrumbs">{parent}<h1>{re.escape(place.page_title)}</h1><', page)


def test_the_guide_page_is_gone(client):
    assert client.get("/ops/admin/competition/guide/").status_code == 404
    with pytest.raises(NoReverseMatch):
        reverse("admin_competition_guide")


def test_competition_setup_links_scoring_setup_and_orange_checks(client):
    page = client.get(reverse("admin_competition_setup")).content.decode()

    assert f'href="{reverse("scoring:scoring_config")}"' in page
    assert f'href="{reverse("orange_team:check_list")}"' in page


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


@pytest.mark.parametrize(("end_in_hours", "shown"), [(None, True), (2, True), (-1, False)])
def test_the_scheduled_start_shows_only_while_the_timer_will_still_fire_it(client, end_in_hours, shown):
    from datetime import timedelta

    from django.utils import timezone

    _arrange("start")
    now = timezone.now()
    end = now + timedelta(hours=end_in_hours) if end_in_hours is not None else None
    CompetitionConfig.objects.update_or_create(
        pk=1, defaults={"competition_start_time": now - timedelta(hours=2), "competition_end_time": end}
    )

    page = _flat(_page(client))
    assert ("The schedule starts it at" in page) == shown
    assert (", or you can start it below." in page) == shown


def test_the_scheduled_stop_shows_above_the_stop_buttons_while_running(client):
    from datetime import timedelta

    from django.utils import timezone

    _arrange("running")
    CompetitionConfig.objects.filter(pk=1).update(competition_end_time=timezone.now() + timedelta(hours=2))

    page = _flat(_page(client))
    assert page.count(", or you can stop it below.") == 1
    assert page.index(", or you can stop it below.") > page.index('id="controls"')


@pytest.mark.parametrize("stage", ALL_STAGES)
def test_the_page_is_built_from_the_standard_modules_and_table(client, stage):
    _arrange(stage)

    page = _flat(_page(client))

    for title in ("Event", "Steps", "Competition Controls"):
        assert re.search(rf'<div[^>]*class="module[^"]*"[^>]*><h2>{title}</h2>', page), title
    # The table component the Teams list uses
    assert re.search(r'<div\s+class="results"><table id="steps_list"\s+aria-label="[^"]+"\s+class="w-full">', page)
    for custom in ("event-card", "step-grid", "next-step", "step-notes", "live-number", "grid-3", "details"):
        assert custom not in page, custom


# The modules that import the standings or the scorecard builder by name
SCORECARD_MODULES = ("registration.services", "scoring.export", "scoring.views.export", "scoring.views.leaderboard")


@pytest.mark.parametrize("stage", ALL_STAGES)
def test_rendering_the_page_builds_no_scorecard_pdf_or_email(client, stage):
    _arrange(stage)
    # Every name the scorecards are built through: the standings, the scorecard, its PDF and the email
    builders = [
        *(f"{module}.compute_standings" for module in SCORECARD_MODULES),
        *(f"{module}.build_scorecard_context" for module in SCORECARD_MODULES if module != "scoring.export"),
        "scoring.calculator.compute_standings",
        "scoring.scorecard.build_scorecard_context",
        "weasyprint.HTML",
        "django.core.mail.EmailMessage.send",
    ]

    with ExitStack() as stack:
        mocks = [stack.enter_context(patch(name, side_effect=AssertionError(name))) for name in builders]
        _page(client)

    for name, mock in zip(builders, mocks, strict=True):
        assert not mock.called, name


def _last_run(accounts_failed):
    CompetitionConfig.objects.filter(pk=1).update(last_run_incomplete=bool(accounts_failed))


@pytest.mark.parametrize("accounts_failed", [0, 1])
def test_while_running_start_is_offered_again_only_after_a_start_that_left_something_off(client, accounts_failed):
    _arrange("running")
    _last_run(accounts_failed)

    page = _flat(_page(client))

    assert ('@click="startCompetition"' in page) == bool(accounts_failed)
    assert ("The last start didn't turn everything on" in page) == bool(accounts_failed)
    assert page.count('@click="stopCompetition"') == 1


@pytest.mark.parametrize("accounts_failed", [0, 1])
def test_while_stopped_stop_is_offered_again_only_after_a_stop_that_left_something_on(client, accounts_failed):
    _arrange("played")
    _last_run(accounts_failed)

    page = _flat(_page(client))

    assert ('@click="stopCompetition"' in page) == bool(accounts_failed)
    assert ("The last stop didn't turn everything off" in page) == bool(accounts_failed)
    assert page.count('@click="startCompetition"') == 1
