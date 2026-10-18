"""Regression tests for bugs the bot's property tests found, each pinned with the input that showed it. Each example
runs a whole handler, so they run the view tests' fewer examples (the -views profile)."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from bot.cogs.admin_competition import AdminCompetitionCog
from bot.cogs.admin_teams import AdminTeamsCog
from bot.cogs.admin_tickets import AdminTicketsCog
from bot.cogs.help_panels import TeamHelpView, TicketButton
from bot.cogs.scoring import ScoringCog
from bot.cogs.ticketing import TicketingCog
from bot.tests.bot_harness import (
    EVERYONE,
    Attachment,
    Chosen,
    Command,
    Item,
    Listener,
    Modal,
    Run,
    World,
    dashboard_view,
    discord_world,
    drive,
    embed_problems,
    may,
    mention_problems,
    on_message,
    on_message_edit,
    option_text,
    sent_problems,
    stored_text,
)
from bot.ticket_dashboard import ResolveTicketModal, format_ticket_embed
from bot.unified_dashboard import DashboardControlView
from core.tests.strategies import file_bytes, file_names, rolled_back
from core.tests.views_harness import views_profile

BROADCAST = Command(AdminCompetitionCog, AdminCompetitionCog.admin_broadcast.callback, may("admin"))
RESOLVE = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_resolve.callback, may("ticketing_support"))
REASSIGN = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_reassign.callback, may("ticketing_admin"))
TICKET = Command(TicketingCog, TicketingCog.create_ticket.callback, frozenset({"blue_team", "other_team"}))
ADMIN_TICKET = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_create.callback, may("ticketing_admin"))
CHANGE_CATEGORY = Command(AdminTicketsCog, AdminTicketsCog.admin_change_category.callback, may("ticketing_admin"))
RESOLVE_MODAL = Modal(ResolveTicketModal, lambda run, data: resolve_modal(run, "claimed"), may("ticketing_support"))
THREAD_MESSAGE = Listener(TicketingCog, TicketingCog.on_message, on_message)
THREAD_EDIT = Listener(TicketingCog, TicketingCog.on_message_edit, on_message_edit)


def resolve_modal(run: Run, name: str) -> ResolveTicketModal:
    from core.tickets_config import get_category_config

    ticket = run.world.tickets[name]
    return ResolveTicketModal(ticket, get_category_config(ticket.category_id) or {})


def in_a_ticket_thread(world: World, content: str, **more: object) -> Chosen:
    thread = world.tickets["open"].discord_thread_id
    return Chosen({"role": "blue_team", "channel": "ticket thread", "thread": thread, "content": content, **more})


@example(message="@everyone the scoring engine is back")
@views_profile()
@given(message=option_text(1, 1000))
def test_an_announcement_pings_only_the_blueteam_role(bot_world: World, message: str) -> None:
    """The bot set no default allowed mentions, so any message it sent pinged everything it named: @everyone in a
    broadcast, and every user an ops log line mentioned."""
    drive(BROADCAST, bot_world, Chosen({"role": "admin", "target": "announcements", "message": message}))


@example(ticket_number="T001-001\x00")
@views_profile()
@given(ticket_number=option_text(1, 20).map(lambda s: s + "\x00"))
def test_an_option_with_a_nul_is_refused(bot_world: World, ticket_number: str) -> None:
    """PostgreSQL text can't hold a NUL, so a ticket number with one failed the lookup (ValueError) and the command
    never answered."""
    drive(RESOLVE, bot_world, Chosen({"role": "admin", "ticket_number": ticket_number}))


@example(volunteer="not a user")
@views_profile()
@given(volunteer=option_text(1, 100).filter(lambda s: not s.strip().isdigit()))
def test_an_option_the_bot_cannot_read_is_answered(bot_world: World, volunteer: str) -> None:
    """A volunteer that isn't a user or an id fails the option's transformer, and the command tree only logged it:
    Discord showed "The application did not respond"."""
    drive(REASSIGN, bot_world, Chosen({"role": "admin", "ticket_number": "T001-002", "volunteer": volunteer}))


@example(content="logs\x00")
@views_profile()
@given(content=option_text(1, 2000).map(lambda s: s + "\x00"))
def test_a_thread_message_with_a_nul_is_saved_without_it(bot_world: World, content: str) -> None:
    """A ticket thread message holding a NUL failed to save as a comment (ValueError), so the listener raised."""
    drive(THREAD_MESSAGE, bot_world, in_a_ticket_thread(bot_world, content))
    drive(THREAD_EDIT, bot_world, in_a_ticket_thread(bot_world, content, **{"message id": 1, "was saved": True}))


@example(category="Box Reset")
@views_profile()
@given(category=option_text(1, 100))
def test_a_category_that_is_not_a_number_is_refused(bot_world: World, category: str) -> None:
    """Every command that files a ticket or changes its category read the option with int() and raised on
    anything else (autocomplete offers ids, but Discord sends whatever was typed)."""
    drive(TICKET, bot_world, Chosen({"role": "blue_team", "category": category, "description": "Down"}))
    drive(
        ADMIN_TICKET, bot_world, Chosen({"role": "admin", "team_number": 1, "category": category, "description": "x"})
    )
    drive(CHANGE_CATEGORY, bot_world, Chosen({"role": "admin", "ticket_number": "T001-002", "new_category": category}))


@example(hostname="web" * 100)
@views_profile()
@given(hostname=option_text(256, 6000))
def test_a_ticket_field_longer_than_its_column_is_refused(bot_world: World, hostname: str) -> None:
    """A slash command option has no length below Discord's 6000 unless it declares one, so a long hostname, service
    or IP reached the database's varchar column (DataError)."""
    chosen = {"role": "blue_team", "category": "2", "description": "Down", "hostname": hostname}
    drive(TICKET, bot_world, Chosen(chosen))


@example(description="Help")
@views_profile()
@given(description=option_text(1, 1000))
def test_a_team_files_only_in_a_category_teams_may_create(bot_world: World, description: str) -> None:
    """/ticket took any category id, so a team could file a ticket in a staff-only category the web form hides."""
    from ticketing.models import TicketCategory

    with rolled_back():
        TicketCategory.objects.filter(pk=6).update(user_creatable=False)
        run = drive(TICKET, bot_world, Chosen({"role": "blue_team", "category": "6", "description": description}))
    assert "ticketing_ticket" not in run.written


@example(description="need reset")
@views_profile()
@given(description=stored_text(200).filter(str.strip))
def test_ticket_commands_require_the_categorys_required_fields(bot_world: World, description: str) -> None:
    """/tickets create never checked the category's required fields, and /ticket let an unknown hostname without an
    IP satisfy a required IP; a hostname Quotient knows satisfies a required IP in commands and modals alike."""
    from unittest.mock import patch

    from bot.cogs.help_panels import BoxResetModal

    def build_box_modal(run: Run, data: object) -> BoxResetModal:
        modal = BoxResetModal()
        modal.category_id = "2"
        return modal

    box_modal = Modal(BoxResetModal, build_box_modal, frozenset({"blue_team"}))
    infra = (["web01"], {"web01": "10.0.1.5"}, [])
    with patch.object(TicketingCog, "_load_infrastructure_data", return_value=infra):
        admin = drive(
            ADMIN_TICKET,
            bot_world,
            Chosen({"role": "admin", "team_number": 1, "category": "2", "description": description}),
        )
        team = drive(
            TICKET,
            bot_world,
            Chosen({"role": "blue_team", "category": "2", "description": description, "hostname": "unknown01"}),
        )
        unknown_modal = drive(
            box_modal, bot_world, Chosen({"role": "blue_team", "Hostname": "unknown01", "IP Address": ""})
        )
        known_modal = drive(box_modal, bot_world, Chosen({"role": "blue_team", "Hostname": "web01", "IP Address": " "}))
        admin_ok = drive(
            ADMIN_TICKET,
            bot_world,
            Chosen(
                {"role": "admin", "team_number": 1, "category": "2", "description": description, "hostname": "web01"}
            ),
        )
    assert "ticketing_ticket" not in admin.written
    assert "ticketing_ticket" not in team.written
    assert "ticketing_ticket" not in unknown_modal.written
    assert "ticketing_ticket" in known_modal.written
    assert "ticketing_ticket" in admin_ok.written


@example(points=2**31)
@views_profile()
@given(points=st.integers(2**31, 2**53))
def test_resolve_points_past_the_column_are_refused(bot_world: World, points: int) -> None:
    """/tickets resolve took any integer Discord sends (to 2**53) as the points charged, past their integer column
    (DataError)."""
    drive(RESOLVE, bot_world, Chosen({"role": "admin", "ticket_number": "T001-002", "points": points}))


@example(points="１２")
@views_profile()
@given(points=option_text(1, 5))
def test_resolve_modal_points_are_read_as_the_web_reads_them(bot_world: World, points: str) -> None:
    """The resolve modal read its points with int(), unlike the web form, and a NUL in its notes reached the
    database."""
    drive(RESOLVE_MODAL, bot_world, Chosen({"role": "admin", "Resolution Notes": "Fixed\x00", "Points": points}))


@pytest.mark.parametrize("name", ["resolve", "cancel", "change-category", "reassign", "reopen"])
def test_a_ticket_number_option_is_as_long_as_the_column(name: str) -> None:
    """The ticket number options took up to Discord's 6000 characters, and "Ticket ... not found" repeated all of
    them, past the 2000 a message takes."""
    from ticketing.models import Ticket

    command = next(c for c in AdminTicketsCog.tickets_group.walk_commands() if c.name == name)
    param = next(p for p in command.parameters if p.name == "ticket_number")
    assert param.max_value == Ticket._meta.get_field("ticket_number").max_length


ACTIVATE = Command(AdminTeamsCog, AdminTeamsCog.admin_activate_teams.callback, may("admin"))
SET_APPS = Command(AdminCompetitionCog, AdminCompetitionCog.admin_competition_set_apps.callback, may("admin"))
REOPEN = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_reopen.callback, may("ticketing_admin"))


@example(teams="1," + "x" * 3000)
@views_profile()
@given(teams=option_text(2000, 6000))
def test_a_team_range_error_repeats_only_the_start_of_the_range(bot_world: World, teams: str) -> None:
    """parse_team_range's error repeated the whole part it couldn't read, and the reply went past 2000 characters."""
    drive(ACTIVATE, bot_world, Chosen({"role": "admin", "teams": teams}))


@example(app_slugs="netbird," + "x" * 3000)
@views_profile()
@given(app_slugs=option_text(2000, 6000))
def test_app_slugs_are_read_as_the_competition_page_reads_them(bot_world: World, app_slugs: str) -> None:
    """/competition set-apps stored any text as application slugs (the competition page takes only slugs) and
    repeated them all in its reply, past 2000 characters."""
    drive(SET_APPS, bot_world, Chosen({"role": "admin", "app_slugs": app_slugs}))


@example(reason="r" * 3000)
@views_profile()
@given(reason=option_text(2000, 6000))
def test_a_reopen_reply_repeats_only_the_start_of_the_reason(bot_world: World, reason: str) -> None:
    """The reopen reply repeated the whole reason, past the 2000 characters a message takes."""
    drive(REOPEN, bot_world, Chosen({"role": "admin", "ticket_number": "T001-003", "reason": reason}))


@example(target="announcements", message="m" * 3000)
@example(target="1", message="m" * 3000)
@views_profile()
@given(target=st.sampled_from(("announcements", "1", "all-teams")), message=option_text(1900, 6000))
def test_a_long_broadcast_is_sent_in_parts(bot_world: World, target: str, message: str) -> None:
    """A broadcast over 2000 characters (with its header) went out as one message, which Discord refuses."""
    drive(BROADCAST, bot_world, Chosen({"role": "admin", "target": target, "message": message}))


@example(target="1", message="m" * 3000)
@views_profile()
@given(target=st.sampled_from(("announcements", "1")), message=option_text(1900, 6000))
def test_a_long_web_broadcast_is_sent_in_parts(bot_world: World, target: str, message: str) -> None:
    """The queue sent a broadcast from the portal as one message too, and Discord refused one over 2000."""
    from asgiref.sync import async_to_sync

    from bot.discord_queue import DiscordQueueProcessor
    from core.discord_tasks import BroadcastMessage

    with rolled_back(), discord_world(bot_world, confirm=True) as fakes:
        processor = DiscordQueueProcessor(fakes.bot)
        async_to_sync(processor._handle_broadcast_message)(BroadcastMessage(target=target, message=message, sender="g"))
        assert fakes.sent
        for sent in fakes.sent:
            assert not sent_problems(sent), sent_problems(sent)
            assert not mention_problems(sent, fakes.bot.allowed_mentions)


@example(filename="shot\x00.png", content_type="image/png" + "x" * 200, data=b"<svg onload=alert(1)>")
@views_profile()
@given(filename=file_names(), content_type=st.one_of(st.none(), option_text(0, 300)), data=file_bytes())
def test_a_thread_attachment_is_stored_as_a_web_upload_is(
    bot_world: World, filename: str, content_type: str | None, data: bytes
) -> None:
    """The listener stored an attachment's name and claimed content type as Discord gave them: a NUL or a name past
    255 characters or a content type past 100 failed the insert, and a claimed image/* gave a non-image a broken
    <img> preview on the ticket page."""
    attachment = Attachment(filename, content_type, len(data), data)
    run = drive(THREAD_MESSAGE, bot_world, in_a_ticket_thread(bot_world, "see attached", attachments=[attachment]))
    assert "ticketing_ticketattachment" in run.written


DASHBOARD = Item("button", DashboardControlView.sort_created, dashboard_view, EVERYONE)
TICKET_LIST = Command(AdminTicketsCog, AdminTicketsCog.admin_ticket_list.callback, may("ticketing_support"))
TEAM_LIST = Command(AdminTeamsCog, AdminTeamsCog.admin_teams.callback, may("admin"))
TEAM_INFO = Command(AdminTeamsCog, AdminTeamsCog.admin_team_info.callback, may("admin"))
INCIDENT = Command(ScoringCog, ScoringCog.incident_report.callback, frozenset({"blue_team", "other_team"}))


def long_tickets(text: str, count: int) -> Chosen:
    return Chosen({"hostile rows": True, "a name": "Team", "a text": text, "more categories": 0, "more tickets": count})


@example(text="x" * 5000, count=40)
@views_profile()
@given(text=stored_text(5000), count=st.integers(6, 40))
def test_the_dashboard_fits_one_embed(bot_world: World, text: str, count: int) -> None:
    """The dashboard put a field per category in one embed with no total in mind: many long tickets passed the
    6000 characters an embed takes, and Discord refused the edit (the dashboard froze)."""
    drive(DASHBOARD, bot_world, Chosen({**long_tickets(text, count).values, "role": "admin"}))


@example(text="x" * 5000, count=30)
@views_profile()
@given(text=stored_text(5000), count=st.integers(0, 30))
def test_the_ticket_list_fits_one_embed(bot_world: World, text: str, count: int) -> None:
    """/tickets list named each field with the ticket's title (up to 255 characters, past the 256 a field name
    takes with the number) and added 25 of them, past the 6000 characters an embed takes."""
    drive(TICKET_LIST, bot_world, Chosen({**long_tickets(text, count).values, "role": "admin", "status": "all"}))


@example(name="n" * 100)
@views_profile()
@given(name=stored_text(100).filter(str.strip))
def test_the_team_list_fits_one_embed(bot_world: World, name: str) -> None:
    """/teams list put up to 25 teams in one field, past the 1024 characters a field takes with long names."""
    from team.models import Team

    with rolled_back():
        Team.objects.bulk_create(Team(team_number=n, team_name=name) for n in range(4, 26))
        drive(TEAM_LIST, bot_world, Chosen({"role": "admin"}))


@example(group="")
@views_profile()
@given(group=st.sampled_from(("", " ")))
def test_team_info_shows_a_team_without_an_authentik_group(bot_world: World, group: str) -> None:
    """/teams info showed the Authentik group as a field even when blank, which Discord refuses."""
    from team.models import Team

    with rolled_back():
        Team.objects.filter(team_number=1).update(authentik_group=group)
        drive(TEAM_INFO, bot_world, Chosen({"role": "admin", "team_number": 1}))


@example(hostname=" \u2029", text="x" * 5000)
@views_profile()
@given(hostname=stored_text(255), text=stored_text(5000))
def test_a_ticket_thread_embed_fits(bot_world: World, hostname: str, text: str) -> None:
    """The ticket's thread embed added a blank hostname, service or IP as a field (Discord refuses one), and with
    long fields and a long description passed the 6000 characters an embed takes."""
    from ticketing.models import Ticket

    with rolled_back():
        Ticket.objects.filter(pk=bot_world.tickets["resolved"].pk).update(
            hostname=hostname, service_name=hostname[:100], title=text[:255], description=text, resolution_notes=text
        )
        ticket = Ticket.objects.select_related("team", "assigned_to").get(pk=bot_world.tickets["resolved"].pk)
        assert not embed_problems([format_ticket_embed(ticket)])


@example(source_ip="not an address")
@views_profile()
@given(source_ip=option_text(1, 45))
def test_an_incident_report_is_checked_as_the_model_checks_it(bot_world: World, source_ip: str) -> None:
    """/incident-report saved any text as the source IP (a GenericIPAddressField) and any length of service, so the
    insert failed (the bot answered "Failed to submit" for what was the reporter's typo)."""
    chosen = {
        "role": "blue_team",
        "affected_box": "web",
        "affected_service": "http",
        "source_ip": source_ip,
        "attack_vector": "SQL injection",
        "description": "We saw a SQL injection in the web logs and blocked the address at the firewall.",
    }
    drive(INCIDENT, bot_world, Chosen(chosen))


@example(name="n" * 100)
@views_profile()
@given(name=stored_text(100).filter(lambda s: len(s.strip()) > 80))
def test_thread_names_and_autocomplete_choices_fit_in_100_characters(bot_world: World, name: str) -> None:
    """The category autocomplete named a choice "<category> (<points>pt)", past the 100 characters Discord takes:
    Discord showed no choices."""
    chosen = {"hostile rows": True, "a name": name, "a text": "t", "more categories": 0, "more tickets": 0}
    admin = {**chosen, "role": "admin", "team_number": 1, "category": "6", "description": "x"}
    drive(ADMIN_TICKET, bot_world, Chosen(admin))
    drive(TICKET, bot_world, Chosen({**chosen, "role": "blue_team", "category": "6", "description": "Help"}))


TICKET_BUTTON = Item("button", TicketButton, lambda run, data: TeamHelpView(run.discord.bot), EVERYONE)
RECREATE = Command(AdminTeamsCog, AdminTeamsCog.admin_recreate_teams.callback, may("admin"))


@example(count=30, name="c" * 100)
@views_profile()
@given(count=st.integers(25, 60), name=stored_text(100).filter(str.strip))
def test_the_ticket_panel_and_its_select_fit_many_categories(bot_world: World, count: int, name: str) -> None:
    """The category select offered every category a team may create, and Discord takes 25 options; the ticket
    panel listed them all in its embed, past the 4096 characters a description takes."""
    chosen = {"hostile rows": True, "a name": name, "a text": "t", "more categories": count, "more tickets": 0}
    drive(TICKET_BUTTON, bot_world, Chosen({**chosen, "role": "blue_team"}))
    drive(RECREATE, bot_world, Chosen({**chosen, "role": "admin", "teams": "2"}))
