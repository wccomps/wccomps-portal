"""Property tests for every Django form: whatever a client posts, validation never raises, and a valid form's
cleaned data fits its own fields, is stored by what its view does without a database error, and cleans the same
when posted back.

FORM_CASES holds one case per form; test_property_coverage fails when a form has no case. A case says how its view
builds the form, which values reach its valid branch (hints), and what its view does with the cleaned data (use),
following the view: a real save where the view saves the form or calls a service, otherwise the values it stores,
checked against their columns (assert_storable).
"""

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django import forms
from django.contrib.auth.models import User
from django.contrib.messages.storage.cookie import CookieStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import QuerySet
from django.http import QueryDict
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from hypothesis import given
from hypothesis import strategies as st
from orange_team.forms import (
    AssignmentRejectForm,
    CriterionForm,
    FollowUpForm,
    OrangeCheckForm,
    ReassignForm,
    SubmitScoreForm,
)
from orange_team.models import OrangeAssignment, OrangeCheck
from packets.forms import PacketActionForm, PacketResendForm, PacketUploadForm
from packets.models import Packet, PacketDistribution
from registration.forms import (
    EventForm,
    NewEventFields,
    RegistrationForm,
    RejectRegistrationForm,
    SeasonForm,
    ShownSchoolForm,
)
from registration.models import Event, School, Season, TeamRegistration, collapse_whitespace, normalize_school_name
from registration.services import NewEvent, SchoolTakenError, set_team_school
from scoring.forms import (
    ApproveInjectFeedbackForm,
    IncidentMatchForm,
    IncidentReportForm,
    InjectGradingForm,
    RedTeamIPPoolForm,
    RedTeamScoreForm,
    SaveInjectFeedbackForm,
    ScorecardEmailForm,
    ScoringTemplateForm,
)
from scoring.models import (
    AttackType,
    IncidentReport,
    InjectScore,
    QuotientMetadataCache,
    RedTeamIPPool,
    RedTeamScore,
    ScoringTemplate,
)
from scoring.views.red_team import _create_red_score

from core.admin_views.competition import TIMEZONE_CHOICES
from core.authentik_utils import MAX_TEAMS
from core.forms import (
    ActionForm,
    AppSlugForm,
    BroadcastForm,
    CancelEventForm,
    CloseOutForm,
    LinkConfirmForm,
    ReadinessFixForm,
    ResetPasswordsForm,
    SchoolInfoEditForm,
    SetMaxMembersForm,
    SetScheduleForm,
    StartMessageForm,
    SyncRolesForm,
    TeamActionForm,
    TeamsBulkActionForm,
    TypedConfirmForm,
)
from core.models import AuditLog
from core.tests.strategies import (
    assert_storable,
    column,
    datetime_text,
    form_data,
    long_email,
    request_text,
    resubmission,
    rolled_back,
)
from team.forms import CSVUploadForm, ImportConfirmForm, ImportEventForm, parse_csv_file
from team.models import DiscordLink, LinkToken, Team
from ticketing.forms import (
    CreateTicketForm,
    TicketBoxResetForm,
    TicketBulkActionForm,
    TicketCategoryForm,
    TicketChangeCategoryForm,
    TicketCommentForm,
    TicketReassignForm,
    TicketReopenForm,
    TicketResolveForm,
)
from ticketing.models import Ticket, TicketCategory, parse_playbook_steps

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------------------------------------------
# What the forms' views have in the database
# ---------------------------------------------------------------------------------------------------------------


@dataclass
class World:
    user: User  # a Discord admin, so every page lets them in
    client: Client  # logged in as user
    season: Season
    open_event: Event
    teams: list[Team]
    pool: RedTeamIPPool
    finding: RedTeamScore
    incident: IncidentReport
    template: ScoringTemplate
    registration: TeamRegistration
    pending: OrangeAssignment  # the user's, not yet submitted
    submitted: OrangeAssignment  # the user's, waiting for review
    claimed: Ticket  # claimed by the user
    resolved: Ticket
    grade: InjectScore
    packet: Packet  # sent, with a distribution for team 1


@pytest.fixture
def world(db: Any, create_user_with_groups: Callable[..., User]) -> World:
    user = create_user_with_groups("propertyuser", ["WCComps_Discord_Admin"])
    client = Client()
    client.force_login(user)
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    open_event = Event.objects.create(
        season=season, name="Invitational", event_type="invitational", event_number=1, date=date(2026, 10, 3)
    )
    open_event.open()
    Event.objects.create(
        season=season, name="Qualifier", event_type="qualifier", event_number=1, date=date(2026, 11, 7)
    )
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n:02d}") for n in (1, 2, 3)]
    QuotientMetadataCache.objects.create(
        boxes=[{"name": "web", "ip": "10.0.0.5", "services": [{"name": "web-http", "display_name": "HTTP"}]}],
    )
    pool = RedTeamIPPool.objects.create(name="Pool A", ip_addresses="10.0.0.1", created_by=user)
    finding = RedTeamScore.objects.create(
        attack_type=AttackType.objects.filter(is_active=True).first(),
        source_ip="10.0.0.9",
        submitted_by=user,
        points_per_team=0,
    )
    incident = IncidentReport.objects.create(
        team=teams[0], source_ip="10.0.0.9", attack_detected_at=timezone.now(), attack_description="Seen"
    )
    template = ScoringTemplate.objects.first() or ScoringTemplate.objects.create()
    registration = TeamRegistration.objects.create(school_name="Registered School")
    check = OrangeCheck.objects.create(title="Phones", description="", max_points=10)
    pending = OrangeAssignment.objects.create(orange_check=check, user=user, team=teams[0])
    submitted = OrangeAssignment.objects.create(orange_check=check, user=user, team=teams[1], status="submitted")
    category = TicketCategory.objects.first()
    Team.objects.filter(pk=teams[0].pk).update(ticket_counter=2)  # the two tickets below took numbers 1 and 2
    claimed = Ticket.objects.create(
        team=teams[0], category=category, ticket_number="T001-001", title="Help", status="claimed", assigned_to=user
    )
    resolved = Ticket.objects.create(
        team=teams[0], category=category, ticket_number="T001-002", title="Done", status="resolved"
    )
    grade = InjectScore.objects.create(
        team=teams[0], inject_id="1", inject_name="Inject 01", points_awarded=0, graded_by=user
    )
    set_team_school(teams[0], "School One", contact_email="coach@example.edu")
    packet = Packet.objects.create(
        title="Packet",
        file_data=b"%PDF-1.7",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=8,
        status="completed",
        uploaded_by="gold",
    )
    PacketDistribution.objects.create(packet=packet, team=teams[0])
    return World(
        user, client, season, open_event, teams, pool, finding, incident, template, registration,
        pending, submitted, claimed, resolved, grade, packet,
    )  # fmt: skip


def posts_to(path: Callable[[World], str], **extra: str) -> Use:
    """What the view does with the same POST (and files), driven through the test client: it must not fail."""

    def use(w: World, form: Any) -> None:
        post: dict[str, Any] = {key: form.data.getlist(key) for key in form.data} | extra
        for upload in form.files.values():
            upload.seek(0)
        response = w.client.post(path(w), {**post, **form.files})
        assert response.status_code < 500

    return use


def competition_action(action: str) -> Use:
    return posts_to(lambda w: reverse("admin_competition_action"), action=action)


def _inject_grading_through_the_view(w: World, form: Any) -> None:
    """The grading page lists Quotient's injects; one inject is enough to grade."""
    inject = SimpleNamespace(inject_id=1, title="Inject 01 - Web (100 pts)")
    with patch("quotient.client.QuotientClient.get_injects", return_value=[inject]):
        posts_to(lambda w: reverse("scoring:inject_grading") + "?inject=1")(w, form)


def store(*pairs: tuple[Any, object]) -> None:
    """The view stores each value in that model field: its column takes it."""
    for model_field, value in pairs:
        assert_storable(model_field, value)


def request_for(user: User) -> Any:
    request = RequestFactory().post("/")
    request.user = user
    request._messages = CookieStorage(request)
    return request


# ---------------------------------------------------------------------------------------------------------------
# One case per form
# ---------------------------------------------------------------------------------------------------------------

Post = dict[str, str | list[str]]
Factory = Callable[[QueryDict, dict[str, SimpleUploadedFile]], Any]
# How the view builds the form, as a factory of bound forms; it may draw the view's variant (an admin or not)
Make = Callable[[World, st.DataObject | None], Factory]  # None: the view's usual variant
Hints = Callable[[World], dict[str, st.SearchStrategy[Any]]]
Use = Callable[[World, Any], None]


@dataclass(frozen=True)
class FormCase:
    form: type[forms.BaseForm]
    make: Make | None = None  # default form(data, files)
    hints: Hints | None = None  # values that reach the valid branch
    use: Use | None = None  # what the view does with a valid form; must not raise
    # Why posting the cleaned data back doesn't apply, when it doesn't
    not_idempotent: str = ""
    resubmit: Callable[[dict[str, Any]], QueryDict] = field(default=resubmission)
    # POSTs that once broke the form's use: each runs through the valid-form property before the generated ones
    # (or a function of the world, for a POST that names its rows)
    examples: tuple[Post | Callable[[World], Post], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.form.__module__}.{self.form.__qualname__}"

    def factory(self, w: World, draw: st.DataObject | None) -> Factory:
        return self.make(w, draw) if self.make is not None else self.form


YOLO = st.sampled_from(("yolo", " YOLO ", "Yolo\n", "yolo\x0b"))
TEAM_RANGES = st.one_of(
    st.lists(st.integers(1, MAX_TEAMS), min_size=1, max_size=5).map(lambda ns: ",".join(map(str, ns))),
    st.tuples(st.integers(-5, MAX_TEAMS + 5), st.integers(-5, MAX_TEAMS + 5)).map(lambda t: f"{t[0]}-{t[1]}"),
    st.sampled_from(("1-50", " 1 , 3-5 ,", "1--2", "-1", "5-", "1,,2", "١-٣")),
)


def NEW_EVENT_HINTS(w: World) -> dict[str, st.SearchStrategy[Any]]:  # noqa: N802
    return {
        "new_name": st.text(min_size=1, max_size=255),
        "new_date": st.dates().map(date.isoformat),
        "new_type": st.sampled_from([t for t, _ in Event.EVENT_TYPE_CHOICES]),
        "current_season": st.just(str(w.season.pk)),
    }


def _school_info_use(w: World, form: Any) -> None:
    cd = form.cleaned_data
    with contextlib.suppress(SchoolTakenError):  # the view shows that error
        set_team_school(
            w.teams[0],
            cd["school_name"],
            contact_email=cd["contact_email"],
            secondary_email=cd.get("secondary_email", ""),
            notes=cd.get("notes", ""),
            updated_by="gold",
        )


def _category_use(w: World, form: Any) -> None:
    category = form.save()
    # The playbook is shown as steps: each one non-empty, stripped, and taken from the playbook's text
    for step in category.steps:
        assert step and step == step.strip() and step in category.playbook


def _new_event_use(w: World, form: Any) -> None:
    target = form.target() if isinstance(form, CloseOutForm) else form.event_to_import
    if isinstance(target, NewEvent):
        store(
            (column(Event, "name"), target.name),
            (column(Event, "date"), target.date),
            (column(Event, "event_type"), target.event_type),
        )
        if target.season is not None:
            store((column(Season, "name"), target.season.name), (column(Season, "year"), target.season.year))


def _team_numbers_check(w: World, form: Any) -> None:
    numbers = form.cleaned_data["team_numbers"]
    if numbers is not None:
        assert numbers == sorted(set(numbers)) and all(1 <= n <= MAX_TEAMS for n in numbers)
        store((column(AuditLog, "details"), {"team_numbers": numbers}))


def _red_score_make(w: World, draw: st.DataObject) -> Factory:
    return lambda data, files: RedTeamScoreForm(data, files, user=w.user)


def _incident_make(w: World, draw: st.DataObject) -> Factory:
    is_admin = draw.draw(st.booleans(), label="is_admin") if draw else False
    return lambda data, files: IncidentReportForm(None if is_admin else w.teams[0], is_admin, data, files)


def _incident_match_use(w: World, form: Any) -> None:
    incident = form.save(commit=False)
    incident.approve(w.user)


def _resubmit_team_numbers(cleaned: dict[str, Any]) -> QueryDict:
    data = resubmission({k: v for k, v in cleaned.items() if k != "team_numbers"})
    if cleaned["team_numbers"] is not None:
        # An empty list is what a field of only commas cleans to
        data["team_numbers"] = ",".join(map(str, cleaned["team_numbers"])) or ","
    return data


def _resubmit_ticket_numbers(cleaned: dict[str, Any]) -> QueryDict:
    data = QueryDict(mutable=True)
    data["ticket_numbers"] = ",".join(cleaned["ticket_numbers"])
    return data


def _resubmit_dry_run(cleaned: dict[str, Any]) -> QueryDict:
    data = QueryDict(mutable=True)
    data["dry_run"] = "true" if cleaned["dry_run"] else "false"
    return data


def _tz_hint() -> st.SearchStrategy[str]:
    return st.one_of(st.sampled_from([tz for tz, _ in TIMEZONE_CHOICES]), request_text(40))


def _datetime_local() -> st.SearchStrategy[str]:
    return st.one_of(st.datetimes().map(lambda d: d.strftime("%Y-%m-%dT%H:%M")), datetime_text())


def assert_message_fits(message: str) -> None:
    assert len(message) <= StartMessageForm.MAX_LENGTH and "\r" not in message


def assert_bool(value: object) -> None:
    assert isinstance(value, bool)


def assert_str(value: object) -> None:
    assert isinstance(value, str)


def _save_criterion(form: Any) -> None:
    criterion = form.save(commit=False)
    criterion.orange_check = OrangeCheck.objects.create(title="Check", description="")
    criterion.save()


def _save_pool(w: World, form: Any) -> None:
    pool = form.save(commit=False)
    pool.created_by = w.user
    pool.save()


def _new_event_fields_use(w: World, form: Any) -> None:
    try:
        new_event = form.new_event()
    except forms.ValidationError:
        return
    store((column(Event, "name"), new_event.name), (column(Event, "date"), new_event.date))


def _import_event_make(w: World, draw: st.DataObject) -> Factory:
    season = draw.draw(st.sampled_from([w.season, None]), label="season") if draw else w.season
    return lambda data, files: ImportEventForm(data, files, season=season)


def _create_ticket(w: World, form: CreateTicketForm) -> None:
    from ticketing.utils import create_ticket_atomic

    assert form.category_obj is not None
    cd = form.cleaned_data
    create_ticket_atomic(
        team=w.teams[0],
        category=form.category_obj,
        title=cd["title"],
        description=cd["description"],
        hostname=cd["hostname"],
        ip_address=cd["ip_address"],
        service_name=cd["service_name"],
        enforce_team_limit=False,
    )


CASES = [
    FormCase(
        SchoolInfoEditForm,
        examples=(
            {"school_name": "ß" * 255, "contact_email": "a@b.co"},
            {"school_name": "S", "contact_email": long_email(255)},
        ),
        use=_school_info_use,
        hints=lambda w: {
            "school_name": st.one_of(st.text(min_size=1, max_size=255), st.sampled_from(("ß" * 200, " A  b ")))
        },
    ),
    FormCase(BroadcastForm, use=posts_to(lambda w: reverse("admin_broadcast_action"))),
    FormCase(SetMaxMembersForm, use=competition_action("set_max_members")),
    FormCase(TypedConfirmForm, hints=lambda w: {"confirm": YOLO}),
    FormCase(
        CancelEventForm,
        hints=lambda w: {"confirm": YOLO, "open_event": st.just(str(w.open_event.pk))},
    ),
    FormCase(
        CloseOutForm,
        hints=lambda w: {
            "confirm": YOLO,
            "open_event": st.sampled_from(("", str(w.open_event.pk))),
            **NEW_EVENT_HINTS(w),
        },
        use=_new_event_use,
        not_idempotent="a valid Close Out with no open event names a new event; posted back, its name is taken",
    ),
    FormCase(
        StartMessageForm,
        examples=({"start_message": "a\r\r\nb"}, {"start_message": "0\r"}),
        use=lambda w, f: (
            assert_message_fits(f.cleaned_data["start_message"]),
            competition_action("set_start_message")(w, f),
        ),
    ),
    # Adding an app asks Authentik about it; removing one is the same form and stores the same list
    FormCase(AppSlugForm, use=competition_action("remove_app")),
    FormCase(
        SetScheduleForm,
        hints=lambda w: {
            "start_datetime": _datetime_local(),
            "end_datetime": _datetime_local(),
            "start_timezone": _tz_hint(),
            "end_timezone": _tz_hint(),
        },
        use=competition_action("set_schedule"),
    ),
    FormCase(
        ResetPasswordsForm,
        hints=lambda w: {"team_numbers": TEAM_RANGES},
        use=_team_numbers_check,
        resubmit=_resubmit_team_numbers,
    ),
    FormCase(ActionForm),
    FormCase(ReadinessFixForm),
    FormCase(
        TeamActionForm,
        hints=lambda w: {"action": st.sampled_from(("activate", "deactivate", "unlink_user"))},
        use=lambda w, f: DiscordLink.objects.filter(discord_id=f.cleaned_data["discord_id"] or 0).exists(),
    ),
    FormCase(
        TeamsBulkActionForm,
        hints=lambda w: {"team_numbers": TEAM_RANGES},
        use=_team_numbers_check,
        resubmit=_resubmit_team_numbers,
    ),
    FormCase(LinkConfirmForm, use=lambda w, f: LinkToken.objects.filter(token=f.cleaned_data["token"]).exists()),
    FormCase(SyncRolesForm, use=lambda w, f: assert_bool(f.cleaned_data["dry_run"]), resubmit=_resubmit_dry_run),
    FormCase(
        OrangeCheckForm,
        use=posts_to(lambda w: reverse("orange_team:check_create")),
        examples=(
            {"title": "T", "max_points": "100000000"},
            {"title": "T", "time_limit_minutes": "1440000000000"},
            {"title": "T", "criterion_label_1": "L", "criterion_points_1": "\u00b2"},
            {"title": "T", "criterion_label_1": "L", "criterion_points_1": "99999999999"},
            {"title": "T", "criterion_label_1": "L", "criterion_points_1": "1", "criterion_id_1": "\u00b2"},
        ),
    ),
    FormCase(CriterionForm, use=lambda w, f: _save_criterion(f)),
    FormCase(
        FollowUpForm,
        examples=({"assignment_id": "1", "minutes": "-1065460825"}, {"assignment_id": "1", "minutes": "10000000000"}),
        hints=lambda w: {"assignment_id": st.just(str(w.pending.pk))},
        use=posts_to(lambda w: reverse("orange_team:followup_create")),
    ),
    FormCase(
        AssignmentRejectForm, use=posts_to(lambda w: reverse("orange_team:assignment_reject", args=[w.submitted.pk]))
    ),
    FormCase(ReassignForm, use=lambda w, f: User.objects.filter(pk=f.cleaned_data["user_id"]).exists()),
    FormCase(
        SubmitScoreForm,
        use=posts_to(lambda w: reverse("orange_team:assignment_submit", args=[w.pending.pk])),
    ),
    FormCase(
        PacketUploadForm,
        hints=lambda w: {"team_extras": st.sampled_from(("team,api_key\nTeam01,abc\n", "Team 3,x", ""))},
        use=posts_to(lambda w: reverse("upload_packet")),
        not_idempotent="a file can't be posted back from cleaned data",
    ),
    FormCase(
        PacketActionForm,
        hints=lambda w: {"action": st.sampled_from(("distribute", "test_email", "cancel"))},
        use=lambda w, f: (
            f.cleaned_data["action"] != "test_email"
            or (f.cleaned_data["email"] and f.cleaned_data["team_id"])
            or pytest.fail("test_email passed without an email and a team")
        ),
    ),
    FormCase(
        PacketResendForm,
        examples=({"primary_email": long_email(255)},),
        use=posts_to(lambda w: reverse("packet_resend_team", args=[w.packet.pk, w.teams[0].pk])),
    ),
    FormCase(
        RegistrationForm,
        hints=lambda w: {"agree_to_rules": st.just("on"), "region": st.sampled_from(("wrccdc", "other"))},
        use=lambda w, f: f.save(),
        not_idempotent="a saved registration's form posted back is an edit, which the edit view checks",
    ),
    FormCase(
        SeasonForm,
        hints=lambda w: {"year": st.one_of(st.integers(1900, 2200).map(str), st.just("2026"))},
        use=lambda w, f: f.save(),
    ),
    FormCase(
        EventForm,
        make=lambda w, draw: lambda data, files: EventForm(data, instance=Event(season=w.season)),
        hints=lambda w: {
            "event_number": st.sampled_from(("", "1", "2")),
            "event_type": st.sampled_from(("invitational", "qualifier")),
            "date": st.dates().map(date.isoformat),
        },
        use=lambda w, f: f.save(),
        examples=(
            {
                "name": "Q",
                "event_type": "qualifier",
                "event_number": "1",
                "date": "2026-11-07",
                "start_time": "09:00",
                "end_time": "17:00",
                "max_teams": "50",
            },
        ),
    ),
    FormCase(RejectRegistrationForm, use=lambda w, f: w.registration.reject(f.cleaned_data["reason"])),
    FormCase(ShownSchoolForm, use=lambda w, f: School.objects.filter(pk=f.expected_school_id()).exists()),
    FormCase(
        NewEventFields,
        hints=NEW_EVENT_HINTS,
        use=lambda w, f: _new_event_fields_use(w, f),
    ),
    FormCase(
        RedTeamIPPoolForm,
        make=lambda w, draw: lambda data, files: RedTeamIPPoolForm(data, user=w.user),
        hints=lambda w: {"ip_addresses": st.lists(st.ip_addresses().map(str), min_size=1).map("\n".join)},
        use=lambda w, f: _save_pool(w, f),
    ),
    FormCase(
        RedTeamScoreForm,
        make=_red_score_make,
        hints=lambda w: {
            "source_ip_type": st.sampled_from(("single", "pool")),
            "source_ip": st.ip_addresses().map(str),
            "source_ip_pool": st.just(str(w.pool.pk)),
            "affected_teams": st.just([str(w.teams[0].pk)]),
        },
        use=lambda w, f: _create_red_score(request_for(w.user), f, w.user, []),
    ),
    FormCase(
        IncidentReportForm,
        make=_incident_make,
        hints=lambda w: {
            "source_ip": st.ip_addresses().map(str),
            "destination_ip": st.ip_addresses().map(str),
            "attack_detected_at": _datetime_local(),
            "team": st.just(str(w.teams[0].pk)),
        },
        use=posts_to(lambda w: reverse("scoring:submit_incident_report")),
    ),
    FormCase(
        IncidentMatchForm,
        make=lambda w, draw: (
            lambda data, files: IncidentMatchForm(
                RedTeamScore.objects.filter(pk=w.finding.pk), data, instance=w.incident
            )
        ),
        use=_incident_match_use,
    ),
    FormCase(
        ScoringTemplateForm,
        make=lambda w, draw: lambda data, files: ScoringTemplateForm(data, instance=w.template),
        hints=lambda w: {
            "service_weight": st.just("40"),
            "inject_weight": st.just("40"),
            "orange_weight": st.sampled_from(("20", "20.00", "20.001")),
        },
        use=lambda w, f: f.save(),
    ),
    FormCase(
        InjectGradingForm,
        make=lambda w, draw: lambda data, files: InjectGradingForm(data, team_numbers=[t.team_number for t in w.teams]),
        use=_inject_grading_through_the_view,
    ),
    FormCase(
        SaveInjectFeedbackForm,
        hints=lambda w: {"score_id": st.just(str(w.grade.pk))},
        use=posts_to(lambda w: reverse("scoring:save_inject_feedback")),
    ),
    FormCase(
        ApproveInjectFeedbackForm, use=lambda w, f: InjectScore.objects.filter(pk=f.cleaned_data["score_id"]).exists()
    ),
    FormCase(ScorecardEmailForm, use=lambda w, f: assert_str(f.get_custom_message())),
    FormCase(
        CSVUploadForm,
        hints=lambda w: {"csv_file": st.just(SimpleUploadedFile("s.csv", b"school_name,contact_email\nA,a@b.co\n"))},
        use=lambda w, f: parse_csv_file(f.cleaned_data["csv_file"]),
        not_idempotent="a file can't be posted back from cleaned data",
    ),
    FormCase(
        ImportEventForm,
        make=_import_event_make,
        hints=lambda w: {
            "event": st.sampled_from(("new", str(w.open_event.pk), str(w.open_event.pk), "")),
            "mode": st.sampled_from(("replace", "add", "")),
            "new_season_name": st.one_of(st.just(""), st.just(""), st.text(min_size=1, max_size=100)),
            "new_season_year": st.one_of(st.just(""), st.just(""), st.integers(1990, 2110).map(str)),
            **NEW_EVENT_HINTS(w),
        },
        use=_new_event_use,
        not_idempotent="the season the page showed is not posted; the view passes it",
        # One valid post, so the valid branch is reached whatever the generated examples are: Hypothesis draws text
        # from string constants in the loaded code, so a change anywhere can change them
        examples=(lambda w: {"event": str(w.open_event.pk), "current_season": str(w.season.pk)},),
    ),
    FormCase(ImportConfirmForm),
    FormCase(
        CreateTicketForm,
        hints=lambda w: {
            "category": st.sampled_from(
                [str(pk) for pk in TicketCategory.objects.filter(user_creatable=True).values_list("pk", flat=True)]
            ),
            "description": st.text(min_size=1, max_size=200),
            "hostname": st.text(min_size=1, max_size=50),
            "ip_address": st.ip_addresses().map(str),
            "service_name": st.text(min_size=1, max_size=50),
        },
        use=lambda w, f: _create_ticket(w, f),
    ),
    FormCase(TicketCommentForm, use=posts_to(lambda w: reverse("ticket_comment", args=[w.claimed.ticket_number]))),
    FormCase(
        TicketReassignForm,
        use=lambda w, f: User.objects.filter(username=f.cleaned_data["new_assignee_username"]).exists(),
    ),
    FormCase(
        TicketResolveForm,
        examples=({"points_override": "2147483648"},),
        use=posts_to(lambda w: reverse("ticket_resolve", args=[w.claimed.ticket_number])),
    ),
    FormCase(
        TicketReopenForm,
        use=posts_to(lambda w: reverse("ticket_reopen", args=[w.resolved.ticket_number])),
    ),
    FormCase(
        TicketChangeCategoryForm,
        use=lambda w, f: TicketCategory.objects.filter(pk=f.cleaned_data["new_category"]).exists(),
    ),
    FormCase(
        TicketCategoryForm,
        use=_category_use,
        examples=({"display_name": "X", "points": "2147483648"}, {"display_name": "X", "points": "0"}),
    ),
    FormCase(
        TicketBulkActionForm,
        use=lambda w, f: Ticket.objects.filter(ticket_number__in=f.cleaned_data["ticket_numbers"]).exists(),
        resubmit=_resubmit_ticket_numbers,
    ),
    FormCase(
        TicketBoxResetForm,
        hints=lambda w: {"snapshot": st.sampled_from(("clean", "pre_comp", "snap-1", "current", "../bad"))},
        examples=({"snapshot": "clean"},),
        use=lambda w, f: assert_str(f.cleaned_data["snapshot"]),
    ),
]
FORM_CASES = {case.key: case for case in CASES}


# ---------------------------------------------------------------------------------------------------------------
# The properties
# ---------------------------------------------------------------------------------------------------------------


def _draw_form(case: FormCase, w: World, data: st.DataObject, *, toward_valid: bool) -> tuple[Any, Factory]:
    factory = case.factory(w, data)
    shape = factory(None, None)  # unbound, with the fields as the view builds them, to derive the data from
    hints = case.hints(w) if case.hints else {}
    post, files = data.draw(form_data(shape, hints, toward_valid=toward_valid), label="post")
    return factory(post, files), factory


def _comparable(cleaned: dict[str, Any]) -> dict[str, Any]:
    def plain(value: Any) -> Any:
        if isinstance(value, QuerySet):
            return sorted(o.pk for o in value)
        if hasattr(value, "pk") and hasattr(value, "_meta"):
            return value.pk
        if isinstance(value, SimpleUploadedFile):
            return value.name
        return value

    return {k: plain(v) for k, v in cleaned.items()}


def _check_own_constraints(form: Any) -> None:
    """Each cleaned value is within its field's own limits (a clean_* method can't widen them)."""
    for name, form_field in form.fields.items():
        value = form.cleaned_data.get(name)
        if isinstance(form_field, forms.CharField) and form_field.max_length and isinstance(value, str):
            assert len(value) <= form_field.max_length, f"{name} is longer than its max_length"
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            minimum = getattr(form_field, "min_value", None)
            maximum = getattr(form_field, "max_value", None)
            assert minimum is None or value >= minimum, f"{name} is below its min_value"
            assert maximum is None or value <= maximum, f"{name} is above its max_value"


@pytest.mark.parametrize("case", CASES, ids=list(FORM_CASES))
def test_any_post_validates_without_raising(case: FormCase, world: World) -> None:
    """Any POST (hostile values in every field, fields missing, posted twice, or extra) gets an answer from
    is_valid(), never an exception, and every error is a non-empty message."""

    @given(data=st.data())
    def run(data: st.DataObject) -> None:
        with rolled_back():
            form, _ = _draw_form(case, world, data, toward_valid=False)
            form.is_valid()
            for name, messages in form.errors.items():
                assert isinstance(name, str)
                assert all(isinstance(m, str) and m for m in messages)

    run()


@pytest.mark.parametrize("case", CASES, ids=list(FORM_CASES))
def test_valid_post_fits_its_fields_is_stored_and_cleans_the_same(case: FormCase, world: World) -> None:
    """A valid form's cleaned data is within its fields' limits, its view's use of it (saves, stores, lookups)
    raises nothing, and posting the cleaned data back gives the same cleaned data. At least one example must
    reach the valid branch, so the hints can't silently stop working."""
    valid_examples = []

    def check(form: Any, factory: Factory) -> None:
        if not form.is_valid():
            return
        valid_examples.append(form)
        _check_own_constraints(form)
        if not case.not_idempotent:
            again = factory(case.resubmit(form.cleaned_data), {})
            assert again.is_valid(), f"posted back, the cleaned data is refused: {dict(again.errors)}"
            assert _comparable(again.cleaned_data) == _comparable(form.cleaned_data)
        if case.use is not None:
            case.use(world, form)

    for example in case.examples:
        pinned = example(world) if callable(example) else example
        post = QueryDict(mutable=True)
        for key, value in pinned.items():
            post.setlist(key, value if isinstance(value, list) else [value])
        with rolled_back():
            factory = case.factory(world, None)
            check(factory(post, {}), factory)

    @given(data=st.data())
    def run(data: st.DataObject) -> None:
        with rolled_back():
            form, factory = _draw_form(case, world, data, toward_valid=True)
            check(form, factory)

    run()
    assert valid_examples, f"no example reached {case.key}'s valid branch: give it hints"


# ---------------------------------------------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------------------------------------------


@given(name=request_text(255))
def test_school_name_key_is_idempotent_and_ignores_case_and_spacing(name: str) -> None:
    key = normalize_school_name(name)
    assert normalize_school_name(key) == key
    assert normalize_school_name(f"  {name.upper()}  ") == normalize_school_name(name.upper())
    assert collapse_whitespace(collapse_whitespace(name)) == collapse_whitespace(name)
    assert collapse_whitespace(name) == collapse_whitespace(name).strip()


@given(text=request_text(2000))
def test_playbook_steps_are_stripped_non_empty_lines(text: str) -> None:
    steps = parse_playbook_steps(text)
    assert all(step and step == step.strip() for step in steps)
    assert len(steps) <= len(text.splitlines())
