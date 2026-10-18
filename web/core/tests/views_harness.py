"""Driving every URL pattern with hostile requests, as every role, and checking what comes back.

A view module's property tests hold a table of ViewCase rows, one per URL pattern: how to build its path, query
string and body, and which roles may get past its permission check. The properties (check_answer) hold for every
row: no 5xx and every stream read to the end; a role the row refuses is sent to sign in (signed out) or answered 403
(signed in); a refused request (or one answered 4xx) changes nothing it can see (no row in any table, no mail sent, no
request to another service; the cache isn't checked); every refusal and error is a JSON error exactly when
core.errors.wants_json_error says the request wants one (a JSON endpoint's, or a fetch's), and the page otherwise;
text a request carries is HTML-escaped wherever a page echoes it and never runs as template syntax; and no redirect
leaves the site.

test_property_coverage fails when a URL pattern has no row (and isn't excepted with a reason).
"""

import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote, urlencode, urlsplit

import httpx
from django import forms
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import constants as message_levels
from django.core import mail
from django.core.exceptions import SuspiciousOperation
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, models
from django.http import HttpResponseBase
from django.http.multipartparser import MultiPartParserError
from django.test import Client
from django.test.client import BOUNDARY, MULTIPART_CONTENT, RequestFactory, encode_multipart
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from hypothesis import given, settings
from hypothesis import strategies as st
from orange_team.models import (
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
    OrangeCheckIn,
    OrangeFollowUp,
)
from packets.models import Packet, PacketDistribution
from registration.models import (
    Event,
    EventTeamAssignment,
    RegistrationContact,
    RegistrationEventEnrollment,
    Season,
    TeamRegistration,
)
from registration.services import set_team_school
from scoring.models import (
    AttackType,
    IncidentReport,
    IncidentScreenshot,
    InjectScore,
    OrangeTeamScore,
    QuotientMetadataCache,
    RedTeamIPPool,
    RedTeamScore,
    RedTeamScreenshot,
    ScoringExclusion,
    ScoringTemplate,
    ServiceScore,
)

from core.errors import is_json_endpoint, wants_json_error
from core.models import AuditLog, BotState, CompetitionConfig, DiscordTask
from core.permission_constants import check_groups_for_permission
from core.tests.roles import ROLE_GROUPS, role_user
from core.tests.strategies import PAYLOADS, field_values, request_text, rolled_back, uploaded_files, valid_values
from team.models import LinkAttempt, LinkRateLimit, LinkToken, Team
from ticketing.models import CommentRateLimit, Ticket, TicketAttachment, TicketCategory, TicketComment, TicketHistory

# ---------------------------------------------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------------------------------------------

EVERYONE = frozenset(ROLE_GROUPS)
SIGNED_IN = EVERYONE - {"anonymous"}


def may(*permissions: str) -> frozenset[str]:
    """The roles whose groups grant any of these permissions (core.permission_constants)."""
    return frozenset(
        role
        for role, groups in ROLE_GROUPS.items()
        if groups is not None and any(check_groups_for_permission(groups, p) for p in permissions)
    )


# ---------------------------------------------------------------------------------------------------------------
# The rows the views look up
# ---------------------------------------------------------------------------------------------------------------


@dataclass
class World:
    users: dict[str, User]  # signed-in roles
    sessions: dict[str, str]  # each signed-in role's session key
    season: Season
    event: Event  # the open event
    teams: list[Team]  # teams 1-3; the blue_team role's is team 1 (it holds that team's Authentik group)
    rows: dict[str, list[Any]] = field(default_factory=dict)  # by kind: the valid ids a path can name

    def ids(self, kind: str) -> list[Any]:
        """The valid values of a kind of path parameter ("pk:<app_label.Model>" for an admin model's keys). A kind
        with none fails: a row whose path names no real row only ever drives the view's not-found answer."""
        values = self.rows[kind]
        assert values, f"the world has no {kind}: seed one in build_world"
        return values


def build_world() -> World:
    users = {
        role: role_user(role, username=f"prop-{role}") for role, groups in ROLE_GROUPS.items() if groups is not None
    }
    sessions = {}
    for role, user in users.items():
        signing_in = Client()
        signing_in.force_login(user)
        sessions[role] = signing_in.cookies["sessionid"].value

    season = Season.objects.create(name="2026", year=2026, is_active=True)
    event = Event.objects.create(
        season=season, name="Invitational", event_type="invitational", event_number=1, date=date(2026, 10, 3)
    )
    event.open()
    later = Event.objects.create(
        season=season,
        name="Qualifier",
        event_type="qualifier",
        event_number=1,
        date=date(2026, 11, 7),
        registration_open=True,
    )
    teams = [
        Team.objects.get(team_number=1),  # role_user made it, for the blue_team role
        *(Team.objects.create(team_number=n, team_name=f"Team {n:02d}") for n in (2, 3)),
    ]
    set_team_school(teams[0], "School One", contact_email="coach@example.edu")
    set_team_school(teams[1], "School Two", contact_email="coach2@example.edu")

    QuotientMetadataCache.objects.create(
        boxes=[{"name": "web", "ip": "10.0.0.5", "services": [{"name": "web-http", "display_name": "HTTP"}]}],
        services=[{"name": "web-http", "display_name": "HTTP"}],
        team_count=3,
    )
    ScoringTemplate.objects.first() or ScoringTemplate.objects.create()

    # Tickets: team 1's open, claimed (by support) and resolved ones, and team 2's
    category = TicketCategory.objects.get(pk=6)
    tickets = []
    for team, status, number in (
        (teams[0], "open", "T001-001"),
        (teams[0], "claimed", "T001-002"),
        (teams[0], "resolved", "T001-003"),
        (teams[1], "open", "T002-001"),
    ):
        tickets.append(
            Ticket.objects.create(
                team=team,
                category=category,
                ticket_number=number,
                title=f"Ticket {number}",
                status=status,
                assigned_to=users["ticketing_support"] if status != "open" else None,
                assigned_at=timezone.now() if status != "open" else None,
                resolved_at=timezone.now() if status == "resolved" else None,
                points_charged=5 if status == "resolved" else 0,
            )
        )
    Team.objects.filter(pk=teams[0].pk).update(ticket_counter=3)
    Team.objects.filter(pk=teams[1].pk).update(ticket_counter=1)
    attachments = [
        TicketAttachment.objects.create(
            ticket=ticket, file_data=b"%PDF-1.7", filename="a.pdf", mime_type="application/pdf", uploaded_by="x"
        )
        for ticket in (tickets[0], tickets[3])
    ]
    TicketComment.objects.create(ticket=tickets[0], author=users["blue_team"], comment_text="Hello")

    from core.discord_tasks import SyncRoles

    sync = DiscordTask.enqueue(SyncRoles(requested_by="prop-admin", dry_run=True))
    DiscordTask.objects.filter(pk=sync.pk).update(
        status="completed", result={"roles_added": 1, "roles_removed": 0, "errors": 0, "changes": []}
    )
    pending_task = DiscordTask.enqueue(SyncRoles(requested_by="prop-admin", dry_run=False))

    # Red team: findings by red (pending and approved), one by admin that red contributes to, a pool, screenshots
    attack = AttackType.objects.filter(is_active=True).first()
    pool = RedTeamIPPool.objects.create(name="Pool A", ip_addresses="10.0.0.1", created_by=users["red_team"])
    # Each red role edits only its own pools: one for admin too
    admin_pool = RedTeamIPPool.objects.create(name="Pool B", ip_addresses="10.0.0.2", created_by=users["admin"])
    findings = []
    for submitter, approved in ((users["red_team"], False), (users["red_team"], True), (users["admin"], False)):
        finding = RedTeamScore.objects.create(
            attack_type=attack,
            source_ip="10.0.0.9",
            submitted_by=submitter,
            points_per_team=10,
            is_approved=approved,
            notes="Seen <b>",
        )
        finding.affected_teams.set(teams[:2])
        finding.contributors.add(users["red_team"])
        findings.append(finding)
    red_shot = RedTeamScreenshot.objects.create(
        finding=findings[0], file_data=b"\x89PNG\r\n\x1a\n", filename="s.png", mime_type="image/png"
    )

    # Each role deletes only the reports it submitted: one by each role that can submit one
    incidents = [
        IncidentReport.objects.create(
            team=team,
            submitted_by=users[role],
            source_ip="10.0.0.9",
            attack_detected_at=timezone.now(),
            attack_description="Seen",
            affected_boxes=["web"],
        )
        for team, role in (
            (teams[0], "blue_team"),
            *((teams[1], role) for role in sorted(may("gold_team", "white_team"))),
        )
    ]
    incident_shot = IncidentScreenshot.objects.create(
        incident=incidents[0], file_data=b"\x89PNG\r\n\x1a\n", filename="i.png", mime_type="image/png"
    )

    grades = [
        InjectScore.objects.create(
            team=team,
            inject_id="1",
            inject_name="Inject 01 - Web (100 pts)",
            points_awarded=50,
            graded_by=users["white_team"],
            feedback="Good",
            is_approved=approved,
        )
        for team, approved in ((teams[0], True), (teams[1], False))
    ]

    # Orange team: a check with criteria; orange's pending and submitted assignments, lead's approved one
    check = OrangeCheck.objects.create(
        title="Phones", description="Call", max_points=10, scheduled_at=timezone.now(), time_limit=timedelta(hours=1)
    )
    criteria = [
        OrangeCheckCriterion.objects.create(orange_check=check, label=f"C{n}", points=5, sort_order=n) for n in (1, 2)
    ]
    plain = OrangeCheck.objects.create(title="Email", description="", max_points=10)
    assignments = [
        OrangeAssignment.objects.create(orange_check=check, user=users["orange_team"], team=teams[0]),
        OrangeAssignment.objects.create(
            orange_check=check, user=users["orange_team"], team=teams[1], status="submitted"
        ),
        OrangeAssignment.objects.create(orange_check=plain, user=users["orange_team"], team=teams[0]),
        OrangeAssignment.objects.create(orange_check=plain, user=users["orange_team_lead"], team=teams[2]),
        # Each orange role saves only its own assignments: one for gold and admin too
        OrangeAssignment.objects.create(orange_check=check, user=users["admin"], team=teams[2]),
        OrangeAssignment.objects.create(orange_check=plain, user=users["gold_team"], team=teams[1]),
    ]
    for assignment in assignments[:2]:
        for criterion in criteria:
            OrangeAssignmentResult.objects.create(assignment=assignment, criterion=criterion)
    OrangeCheckIn.objects.create(user=users["orange_team"])
    followup = OrangeFollowUp.objects.create(
        user=users["orange_team"], assignment=assignments[0], remind_at=timezone.now(), note="Call back"
    )
    orange_score = OrangeTeamScore.objects.create(
        team=teams[0], submitted_by=users["orange_team"], description="Answered", points_awarded=5
    )

    packet = Packet.objects.create(
        title="Packet",
        file_data=b"%PDF-1.7",
        filename="p.pdf",
        mime_type="application/pdf",
        file_size=8,
        status="completed",
        uploaded_by="gold",
        event=event,
    )
    PacketDistribution.objects.create(packet=packet, team=teams[0])
    draft = Packet.objects.create(
        title="Draft", file_data=b"x", filename="d.txt", mime_type="text/plain", file_size=1, uploaded_by="gold"
    )

    registrations = []
    for status in ("pending", "paid", "approved"):
        registration = TeamRegistration.objects.create(school_name=f"Registered {status}", status=status)
        RegistrationContact.objects.create(
            registration=registration, role="captain", name="Cap", email=f"{status}@example.edu"
        )
        RegistrationEventEnrollment.objects.create(registration=registration, event=event)
        registrations.append(registration)
    link_token = LinkToken.objects.create(
        token="prop-link-token",
        discord_id=8_000_000_001,
        discord_username="linker",
        expires_at=timezone.now() + timedelta(minutes=15),
    )

    world = World(users=users, sessions=sessions, season=season, event=event, teams=teams)
    world.rows = {
        "team_number": [t.team_number for t in teams],
        "team_id": [t.pk for t in teams],
        "user_id": [u.pk for u in users.values()],
        "category_id": list(TicketCategory.objects.values_list("pk", flat=True)),
        "task_id": [sync.pk, pending_task.pk],
        "ticket_number": [t.ticket_number for t in tickets],
        "ticket_id": [t.pk for t in tickets],
        "attachment_id": [a.pk for a in attachments],
        "finding_id": [f.pk for f in findings],
        "red_screenshot_id": [red_shot.pk],
        "pool_id": [pool.pk, admin_pool.pk],
        "incident_id": [i.pk for i in incidents],
        "incident_screenshot_id": [incident_shot.pk],
        "grade_id": [g.pk for g in grades],
        "check_id": [check.pk, plain.pk],
        "criterion_id": [c.pk for c in criteria],
        "assignment_id": [a.pk for a in assignments],
        "followup_id": [followup.pk],
        "orange_score_id": [orange_score.pk],
        "packet_id": [packet.pk, draft.pk],
        "registration_id": [r.pk for r in registrations],
        "edit_token": [r.edit_token for r in registrations],
        "season_id": [season.pk],
        "event_id": [event.pk, later.pk],
        "event_assignment_id": list(EventTeamAssignment.objects.values_list("pk", flat=True)),
        "link_token": [link_token.token],
        "app_label": sorted({model._meta.app_label for model in admin.site._registry}),
        "pk:contenttypes.ContentType": list(ContentType.objects.values_list("pk", flat=True)),
    }
    _seed_admin_only_models(teams, tickets[0], users["admin"])
    for model in admin.site._registry:
        world.rows[f"pk:{model._meta.label}"] = list(model._default_manager.values_list("pk", flat=True))
    return world


def _seed_admin_only_models(teams: list[Team], ticket: Ticket, admin_user: User) -> None:
    """A row for each admin model the views don't need, so its change, delete and history pages show a real one."""
    AuditLog.objects.create(action="seeded", admin_user="prop-admin", target_entity="team", target_id=1)
    BotState.objects.create(key="dashboard_message", value="1")
    CompetitionConfig.get_config()
    LinkAttempt.objects.create(discord_id=1, discord_username="linker", authentik_username="prop-admin", success=False)
    LinkRateLimit.objects.create(discord_id=1)
    TicketHistory.objects.create(ticket=ticket, action="created", actor=admin_user)
    CommentRateLimit.objects.create(ticket=ticket, discord_id=1)
    ServiceScore.objects.create(team=teams[0])
    ScoringExclusion.objects.create(team=teams[2])


# ---------------------------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------------------------

# Path values Django's int converter routes ([0-9]+): past every column's range too
HOSTILE_INTS = (0, 2**31 - 1, 2**31, 2**63 - 1, 2**63, 10**30)
# Redirect targets a link or a header can carry
OFF_SITE = (
    "https://evil.example/",
    "//evil.example/",
    "/\\evil.example/",
    "\\\\evil.example",
    "https://testserver@evil.example/",
    "javascript:alert(1)",
    "http:/evil.example",
    " //evil.example",
    "/%2F%2Fevil.example",
)
# Text that is template syntax: rendered as a template it shows the sum, never the text
TEMPLATE_PROBE = "{{ 123456|add:654321 }}"
TEMPLATE_PROBE_RENDERED = "777777"
# What the sweep puts in every field: it breaks out of a double- or single-quoted attribute and opens a tag
PROBE_TAG = "<p data-probe=1>"
ECHO_PROBE = f"'\">{PROBE_TAG}"
# What a page must never echo raw: each payload with markup in it, and the probe's tag on its own (a writer that
# requotes an attribute changes the quotes in front of it)
MARKUP_PAYLOADS = tuple(p for p in (*PAYLOADS, ECHO_PROBE, PROBE_TAG) if "<" in p or '"' in p)


def hostile_text(max_length: int | None = None) -> st.SearchStrategy[str]:
    return st.one_of(request_text(max_length), st.just(TEMPLATE_PROBE), st.sampled_from(OFF_SITE))


def path_int(valid: list[int]) -> st.SearchStrategy[int]:
    options = [st.sampled_from(HOSTILE_INTS), st.integers(0, 10**6)]
    if valid:
        options = [st.sampled_from(valid), st.sampled_from(valid), *options]
    return st.one_of(*options)


def path_str(valid: list[str], *, slashes: bool = False) -> st.SearchStrategy[str]:
    # Only what the converter routes ([^/]+ for str, .+ for path) can reach the view
    routed = re.compile(".+" if slashes else "[^/]+")
    hostile = hostile_text(64).filter(lambda s: routed.fullmatch(s) is not None)
    return st.one_of(st.sampled_from(valid), st.sampled_from(valid), hostile) if valid else hostile


Fields = dict[str, st.SearchStrategy[Any]]
FieldsFor = Callable[[World], Fields]


def form_fields(form: forms.BaseForm, hints: dict[str, st.SearchStrategy[Any]] | None = None) -> Fields:
    """Strategies for each of a form's fields: values it accepts (or the hint) as often as hostile ones."""
    hints = hints or {}
    return {
        name: st.one_of(hints.get(name, valid_values(f)), field_values(f), hostile_text(getattr(f, "max_length", None)))
        for name, f in form.fields.items()
    }


@dataclass(frozen=True)
class ViewCase:
    """One URL pattern: how to build a request for it, and who may get past its permission check."""

    name: str  # the URL name (namespaced where it is)
    allowed: frozenset[str]  # roles that may get past the view's permission check
    also: tuple[str, ...] = ()  # more URL names the row drives the same way (an admin model's pages)
    covers: str = ""  # the entry point test_property_coverage counts it for, when not its URL pattern
    # Valid values for each path parameter (World.rows kind), when a parameter's name isn't its kind
    params: dict[str, str] = field(default_factory=dict)
    query: FieldsFor | None = None  # query string fields
    post: FieldsFor | None = None  # POST fields (str, list of str, an upload, or a list of uploads)
    methods: tuple[str, ...] = ("GET", "POST")
    # Why the view sends a signed-in visitor to sign in (check_answer fails on that otherwise)
    sends_to_sign_in: str = ""
    # Why the view answers JSON to some requests yet isn't a JSON endpoint (check_answer fails on JSON otherwise)
    json_sometimes: str = ""
    json_body: bool = False  # reads a JSON request body
    # Models whose rows must come out the same when the same POST is sent twice
    idempotent: tuple[type[models.Model], ...] = ()
    idempotent_as: str = "admin"  # the role that sends the repeated POST
    # The URL names every_allowed_role_gets_in drives, when not all of them: the ones behind the portal's own gate
    # (an admin model's changelist, behind the admin site; a model admin's other pages are left to the random
    # requests, which check only that non-admins are refused and nothing fails)
    gate_names: tuple[str, ...] = ()
    # A query a role let in needs to get past the view's own checks (every_allowed_role_gets_in)
    entry_query: Callable[[World], dict[str, list[str]]] | None = None
    # Why the view answers 404 to every role it lets in (every_allowed_role_gets_in can't see one get in)
    finds_none: str = ""
    patches: Callable[[World], ExitStack] | None = None  # what the case needs from outside (Quotient's injects)

    @property
    def route(self) -> str:
        return ROUTES[self.name]

    @property
    def json(self) -> bool:
        """A JSON endpoint (core.errors.json_endpoint): its errors are JSON whoever calls it, and the harness calls it
        as the page's fetch() does."""
        return is_json_endpoint(CALLBACKS[self.name])

    @property
    def entry_point(self) -> str:
        return self.covers or f"url:{self.route}"


def _patterns(
    resolver: URLResolver, prefix: str = "", namespace: str = ""
) -> Iterator[tuple[str, str, Callable[..., object]]]:
    for entry in resolver.url_patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _patterns(entry, route, f"{namespace}{entry.namespace}:" if entry.namespace else namespace)
        elif isinstance(entry, URLPattern) and entry.name:
            yield f"{namespace}{entry.name}", route, entry.callback


class _Routes(dict[str, str]):
    def __missing__(self, name: str) -> str:
        self.update((url_name, route) for url_name, route, _ in _patterns(get_resolver()))
        return self[name]


class _Callbacks(dict[str, Callable[..., object]]):
    def __missing__(self, name: str) -> Callable[..., object]:
        self.update((url_name, view) for url_name, _, view in _patterns(get_resolver()))
        return self[name]


ROUTES = _Routes()  # URL name -> its route as test_property_coverage names URL patterns
CALLBACKS = _Callbacks()  # URL name -> its view


@dataclass
class Drawn:
    role: str
    method: str
    path: str
    query: dict[str, list[str]]
    data: dict[str, Any]  # the form fields sent (multipart for a POST, urlencoded for another method)
    body: bytes | None  # a JSON body
    headers: dict[str, str]
    tz: str | None

    def _query_texts(self) -> list[str]:
        return [text for key, values in self.query.items() for text in (key, *values)]

    @property
    def texts(self) -> list[str]:
        """Every piece of text the request sends (upload names included): for the escaping check."""
        body = [self.body.decode("utf-8", "replace")] if self.body is not None else []
        return [unquote(self.path), *self._query_texts(), *_texts(self.data), *body]

    def carries_nul(self) -> bool:
        """A NUL where core.middleware's NUL refusals look: the path, query and cookies, and a signed-in POST's form
        fields (not upload names, another method's body or a JSON body)."""
        posted = self.method == "POST" and self.role != "anonymous"
        fields = _texts({k: v for k, v in self.data.items() if not _is_upload(v)}) if posted else []
        return any("\x00" in text for text in (unquote(self.path), *self._query_texts(), *fields, self.tz or ""))

    def body_refused(self) -> bool:
        """Whether Django refuses the body as sent (a part's headers past its limit, too many fields): it answers 400
        where RejectNulFieldsMiddleware reads a signed-in POST's form, before the view's role check. Asked of
        Django's own parser."""
        if self.method != "POST" or self.role == "anonymous":
            return False
        body, content_type = encoded(self)
        try:
            RequestFactory().generic("POST", "/", body, content_type).POST  # noqa: B018 - reading it parses the body
        except MultiPartParserError, SuspiciousOperation:
            return True
        return False


def _optional(strategy: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """A field missing, posted once, or posted twice."""
    return st.one_of(st.just(None), strategy, strategy, st.lists(strategy, min_size=2, max_size=2))


METHODS = ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS")
TZ_COOKIES = (
    "UTC",
    "America/Los_Angeles",
    "Pacific/Kiritimati",
    "Etc/GMT-14",
    "../../etc/passwd",
    "Nope/Zone",
    "",
    "U\x00TC",
)


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, SimpleUploadedFile):
        return [value.name or ""]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _texts(v)]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in (*_texts(k), *_texts(v))]
    return []


def route_params(case: ViewCase, name: str) -> list[tuple[str, str, str]]:
    """Each path parameter of the URL name: its converter ("re" for a regex pattern's group), name and kind."""
    route = ROUTES[name]
    found = [*path_params(route), *(("re", param) for param in re.findall(r"\(\?P<(\w+)>", route))]
    return [(converter, param, case.params.get(param, param)) for converter, param in found]


def build(
    case: ViewCase,
    *,
    role: str,
    method: str,
    name: str,
    kwargs: dict[str, Any],
    query: dict[str, list[str]],
    fields: dict[str, Any],
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    tz: str | None = None,
) -> Drawn:
    """The request a row sends: its path, its transport (a JSON body for a JSON-body row; else form fields, multipart
    for a POST, urlencoded without uploads for another method with a body, none for GET, HEAD and OPTIONS) and the
    fetch header a JSON endpoint is called with."""
    path = reverse(name, kwargs=kwargs) if kwargs else reverse(name)
    headers = dict(headers or {})
    if case.json:
        headers["Sec-Fetch-Mode"] = "cors"  # a fetch() from the page, as the JSON endpoints are called
    if case.json_body:
        body = body if body is not None else json.dumps(fields).encode()
        fields = {}
    elif method in ("GET", "HEAD", "OPTIONS"):
        fields = {}
    elif method != "POST":
        fields = {name: value for name, value in fields.items() if not _is_upload(value)}
    return Drawn(role, method, path, query, fields, body, headers, tz)


@st.composite
def requests(
    draw: st.DrawFn, case: ViewCase, world: World, *, role: str | None = None, method: str | None = None
) -> Drawn:
    """A request for the case's URL pattern: any role, method, path values, query, body, headers and cookies."""
    role = role or draw(st.sampled_from(sorted(ROLE_GROUPS)), label="role")
    method = method or draw(st.one_of(st.sampled_from(case.methods), st.sampled_from(METHODS)), label="method")
    name = draw(st.sampled_from((case.name, *case.also)), label="url name")
    kwargs: dict[str, Any] = {}
    for converter, param, kind in route_params(case, name):
        valid = world.ids(kind)
        if converter == "int":
            kwargs[param] = draw(path_int(valid))
        elif converter == "re":  # only a valid value routes
            kwargs[param] = draw(st.sampled_from(valid))
        else:
            kwargs[param] = draw(path_str([str(v) for v in valid], slashes=converter == "path"))

    query_fields = {"next": st.one_of(st.sampled_from(OFF_SITE), hostile_text())}
    if case.query is not None:
        query_fields |= case.query(world)
    query = {}
    for field_name, strategy in query_fields.items():
        value = draw(_optional(strategy), label=f"query {field_name}")
        if value is not None:
            query[field_name] = [str(v) for v in (value if isinstance(value, list) else [value])]

    fields: dict[str, Any] = {}
    body = None
    if case.json_body:
        body = draw(json_bodies(case.post(world) if case.post else {}), label="json body")
    elif case.post is not None:
        for field_name, strategy in case.post(world).items():
            value = draw(_optional(strategy), label=f"post {field_name}")
            if value is not None:
                fields[field_name] = value
        extras = draw(st.dictionaries(st.text(max_size=20), hostile_text(), max_size=2), label="extra fields")
        fields |= {k: v for k, v in extras.items() if k not in fields and k}

    headers = {}
    if draw(st.booleans(), label="fetch"):
        headers["Sec-Fetch-Mode"] = "cors"
    if not case.json and draw(st.booleans(), label="htmx"):  # a JSON endpoint is called by fetch(), never htmx
        headers["HX-Request"] = "true"
    if draw(st.booleans(), label="xhr"):
        headers["X-Requested-With"] = "XMLHttpRequest"
    if draw(st.booleans(), label="asks for json"):
        headers["Accept"] = "application/json"
    if draw(st.booleans(), label="referer"):
        headers["Referer"] = draw(st.one_of(st.sampled_from(OFF_SITE), st.just("http://testserver/tickets/")))
    tz = draw(st.one_of(st.none(), st.sampled_from(TZ_COOKIES)), label="tz cookie")
    return build(
        case, role=role, method=method, name=name, kwargs=kwargs, query=query, fields=fields, body=body,
        headers=headers, tz=tz,
    )  # fmt: skip


def path_params(route: str) -> list[tuple[str, str]]:
    return re.findall(r"<(int|str|path):(\w+)>", route)


def json_bodies(fields: Fields) -> st.SearchStrategy[bytes]:
    """A JSON body: the fields as an object (any of them missing or of another type), any JSON value, deeply nested
    JSON, or bytes that aren't JSON at all."""
    json_values = st.recursive(
        st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), hostile_text(40)),
        lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=10), inner, max_size=3)),
        max_leaves=10,
    )
    objects = st.fixed_dictionaries({}, optional={name: st.one_of(s, json_values) for name, s in fields.items()})
    return st.one_of(
        objects.map(lambda o: json.dumps(o).encode()),
        objects.map(lambda o: json.dumps(o).encode()),
        json_values.map(lambda v: json.dumps(v).encode()),
        st.integers(1, 2000).map(lambda n: ("[" * n + "]" * n).encode()),
        st.binary(max_size=200),
        st.sampled_from((b"", b"{", b"null", b'{"criterion_id": NaN}', b"\xff\xfe{}", b'{"a": 1e999}')),
    )


# ---------------------------------------------------------------------------------------------------------------
# Sending a request and checking the answer
# ---------------------------------------------------------------------------------------------------------------

IGNORED_TABLES = {"django_session"}  # signing in and messages write the session on any request


def table_writes() -> dict[str, tuple[int, int, int]]:
    """Rows inserted, updated and deleted in each table so far in this transaction (PostgreSQL's own counts)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables "
            "WHERE n_tup_ins + n_tup_upd + n_tup_del > 0"
        )
        return {name: tuple(counts) for name, *counts in cursor.fetchall() if name not in IGNORED_TABLES}


@dataclass
class Answer:
    response: HttpResponseBase
    content: bytes
    written: list[str]  # tables the request wrote to
    errors: list[str]  # error messages the request queued
    warnings: list[str] = field(default_factory=list)  # its warning messages
    mailed: int = 0  # emails it sent
    outbound: int = 0  # requests it tried to make to another service
    wants_json: bool = False  # core.errors.wants_json_error for the request: its errors are JSON


def client_for(world: World, role: str, tz: str | None = None) -> Client:
    """A fresh client for the role, sending its session's cookie (unless anonymous) and the tz cookie. A sign-out
    deletes the session row only inside the example's rolled-back savepoint, so every request starts signed in again.

    Cookies go as a raw header: the test client's jar refuses a control character in a value, so a hostile tz
    couldn't be sent."""
    cookies = {"sessionid": world.sessions[role]} if role in world.sessions else {}
    if tz is not None:
        cookies["tz"] = tz
    return Client(HTTP_COOKIE="; ".join(f"{name}={value}" for name, value in cookies.items()))


def encoded(drawn: Drawn) -> tuple[bytes, str]:
    """The request's body as sent, and its content type: the JSON body, a POST's form fields as multipart, another
    method's as urlencoded (build drops them for GET, HEAD and OPTIONS)."""
    if drawn.body is not None:
        return drawn.body, "application/json"
    if drawn.method == "POST":
        for upload in (u for v in drawn.data.values() if _is_upload(v) for u in (v if isinstance(v, list) else [v])):
            upload.seek(0)
        return encode_multipart(BOUNDARY, drawn.data), MULTIPART_CONTENT
    return urlencode(drawn.data, doseq=True).encode(), "application/x-www-form-urlencoded"


def send(world: World, drawn: Drawn) -> Answer:
    client = client_for(world, drawn.role, drawn.tz)
    before = table_writes()
    mail_before, outbound_before = len(mail.outbox), len(OUTBOUND)
    body, content_type = encoded(drawn)
    response = client.generic(
        drawn.method, drawn.path, data=body, content_type=content_type, headers=drawn.headers, query_params=drawn.query
    )
    # A stream is read to the end, as a browser would: any failure in it fails the property
    content = b"".join(response.streaming_content) if response.streaming else response.content
    after = table_writes()
    written = sorted(name for name, counts in after.items() if before.get(name) != counts)
    storage = getattr(response.wsgi_request, "_messages", None)  # type: ignore[attr-defined]
    queued = getattr(storage, "_queued_messages", [])
    errors = [str(m) for m in queued if m.level >= message_levels.ERROR]
    warnings = [str(m) for m in queued if m.level == message_levels.WARNING]
    mailed = len(mail.outbox) - mail_before
    wants_json = wants_json_error(response.wsgi_request)  # type: ignore[attr-defined]
    return Answer(response, content, written, errors, warnings, mailed, len(OUTBOUND) - outbound_before, wants_json)


def _is_upload(value: Any) -> bool:
    return isinstance(value, SimpleUploadedFile) or (
        isinstance(value, list) and bool(value) and all(isinstance(v, SimpleUploadedFile) for v in value)
    )


AUTHENTIK_HOSTS = {"auth.wccomps.org"}


class Kind(StrEnum):
    """What an answer is, as far as the oracle cares."""

    SIGN_IN = "sign in"  # the sign-in gate: a redirect to sign in, an htmx reload, or a fetch's 401
    FORBIDDEN = "forbidden"  # 403
    MISSING = "missing"  # 404
    WRONG_METHOD = "wrong method"  # 405
    REFUSED = "refused"  # a redirect with an error (a form's, an object's state)
    ANSWERED = "answered"


def kind_of(answer: Answer) -> Kind:
    response = answer.response
    status = response.status_code
    location = urlsplit(response.get("Location", "")).path
    if status == 401 or (status == 204 and response.get("HX-Refresh") == "true"):
        return Kind.SIGN_IN
    if 300 <= status < 400 and location.startswith("/auth/login/"):
        return Kind.SIGN_IN
    if status in (403, 404, 405):
        return {403: Kind.FORBIDDEN, 404: Kind.MISSING, 405: Kind.WRONG_METHOD}[status]
    if 300 <= status < 400 and answer.errors:
        return Kind.REFUSED
    return Kind.ANSWERED


# A permission check's answers: signed out, sent to sign in; signed in, the 403; a record another team owns, the 404
PERMISSION_REFUSALS = {Kind.SIGN_IN, Kind.FORBIDDEN, Kind.MISSING}
# Statuses core.errors answers (and the sign-in gate's 401): JSON exactly when the request wants it
ERROR_STATUSES = (400, 401, 403, 404, 405)

# Words a refusal of the visitor uses, in a message or a page ("permission" alone is a page's subject too)
REFUSAL_TEXT = re.compile(
    r"access denied|(do not|don't) have permission|not allowed|not authori[sz]ed|can only \w+ (your|tickets you)", re.I
)


def refuses_visitor(answer: Answer, method: str) -> bool:
    """Whether the answer keeps the visitor out (every_allowed_role_gets_in), rather than showing a page or taking
    the request. A redirect elsewhere with an error is refused on a GET, which posts nothing a form could refuse
    (it may be the visitor or the object's state, as email_scorecards with nothing to send: either way the visitor
    didn't get in there); after a POST with no fields it is the form refusing them, so the visitor got in."""
    kind = kind_of(answer)
    return kind in PERMISSION_REFUSALS or kind is Kind.WRONG_METHOD or (kind is Kind.REFUSED and method == "GET")


def unknown_refusal(answer: Answer) -> str | None:
    """The refusal words in an answer refuses_visitor lets in (its messages or page), or None."""
    said = [*answer.errors, *answer.warnings]
    if answer.response.get("Content-Type", "").startswith(("text/html", "application/json", "text/plain")):
        said.append(answer.content.decode(errors="replace"))
    found = [m.group(0) for text in said if (m := REFUSAL_TEXT.search(text))]
    return f"{answer.response.status_code} saying {found[0]!r}" if found else None


def check_answer(case: ViewCase, drawn: Drawn, answer: Answer) -> None:
    response = answer.response
    status = response.status_code
    where = f"{drawn.role} {drawn.method} {drawn.path}"
    assert status < 500, f"{where} answered {status}: {answer.content[:500]!r}"

    kind = kind_of(answer)
    refused = kind is not Kind.ANSWERED
    if drawn.role != "anonymous" and not case.sends_to_sign_in:
        # A signed-in role sent to sign in is a client that lost its session: every check after this would pass
        assert kind is not Kind.SIGN_IN, f"{where}: a signed-in role was sent to sign in ({status})"
    if drawn.role not in case.allowed:
        # The one refusal: sent to sign in when signed out, the 403 when signed in. Only a NUL, or a body Django
        # refuses, is refused (400) before the view's role check; a wrong method (405) comes first where the method
        # check wraps the role check (require_POST outside require_permission), and after it elsewhere
        early = (status == 400 and (drawn.carries_nul() or drawn.body_refused())) or kind is Kind.WRONG_METHOD
        refusal = Kind.SIGN_IN if drawn.role == "anonymous" else Kind.FORBIDDEN
        location = response.get("Location", "")
        assert kind is refusal or early, f"{where}: a role the page refuses got {status} {location}"
    if refused or 400 <= status < 500:
        assert not answer.written, f"{where} refused ({status}, {answer.errors}) but wrote {answer.written}"
        assert not answer.mailed, f"{where} refused ({status}) but sent {answer.mailed} email(s)"
        assert not answer.outbound, f"{where} refused ({status}) but called another service {answer.outbound} time(s)"

    content_type = response.get("Content-Type", "")
    htmx_reload = kind is Kind.SIGN_IN and status == 204  # the gate reloads an htmx page into sign-in
    has_body = drawn.method != "HEAD"
    if has_body and not htmx_reload and status in ERROR_STATUSES:
        is_json = content_type.startswith("application/json")
        if answer.wants_json:
            assert is_json, f"{where} answered {status} {content_type}, not the JSON error the request wants"
            error = json.loads(answer.content).get("error")
            assert isinstance(error, str) and error, f"{where}: a JSON {status} without an error message"
        else:
            assert not is_json, f"{where} answered a JSON {status} to a request that wants the page"
    if case.json and has_body and content_type.startswith("application/json") and status >= 400:
        assert status in (*ERROR_STATUSES, 409, 413, 429), f"{where}: JSON error status {status}"
    if response.streaming and content_type.startswith("application/x-ndjson"):
        # run_detached turns an operation's exception into a "Failed: ..." line: the stream raised
        failed = [line for line in answer.content.decode().splitlines() if '"Failed: ' in line]
        assert not failed, f"{where}: the streamed operation raised: {failed}"
    if content_type.startswith("application/json") and has_body:
        json.loads(answer.content)
        if 200 <= status < 300:
            assert case.json or case.json_sometimes, f"{where} answers JSON: mark its view core.errors.json_endpoint"

    if content_type.startswith("text/html"):
        page = answer.content.decode("utf-8", "replace")
        for text in drawn.texts:
            for payload in MARKUP_PAYLOADS:
                assert payload not in text or payload not in page, f"{where} echoed {payload!r} unescaped"
            if TEMPLATE_PROBE in text:
                assert TEMPLATE_PROBE_RENDERED not in page, f"{where} rendered request text as a template"

    if 300 <= status < 400:
        location = response.get("Location", "")
        # Its start decides where it goes; Django refuses to judge a URL over 2048 characters at all
        assert url_has_allowed_host_and_scheme(location[:2000], allowed_hosts={"testserver", *AUTHENTIK_HOSTS}), (
            f"{where} redirects off the site: {location!r}"
        )


# ---------------------------------------------------------------------------------------------------------------
# What the views reach outside the database
# ---------------------------------------------------------------------------------------------------------------


OUTBOUND: list[str] = []  # every request a view tried to make to another service, by URL


def _offline(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    OUTBOUND.append(str(request.url))
    raise httpx.ConnectError("offline: property tests reach no other service", request=request)


@contextmanager
def offline() -> Iterator[None]:
    """Every other service down (Authentik, Quotient, Discord webhooks), configured as in production. (Streamed
    operations run inline in the test's transaction, core.utils.run_detached; packet emails still go out from worker
    threads, packets.services, with connections of their own.)"""
    from django.test import override_settings

    with ExitStack() as stack:
        stack.enter_context(patch.object(httpx.HTTPTransport, "handle_request", _offline))
        stack.enter_context(
            override_settings(
                AUTHENTIK_CLIENT_ID="property-client",
                AUTHENTIK_SECRET="property-secret",
                QUOTIENT_USERNAME="property",
                QUOTIENT_PASSWORD="property",
            )
        )
        yield


def uploads(max_files: int = 1) -> st.SearchStrategy[Any]:
    return uploaded_files() if max_files == 1 else st.lists(uploaded_files(), max_size=max_files)


# ---------------------------------------------------------------------------------------------------------------
# The properties, for one table row
# ---------------------------------------------------------------------------------------------------------------


def views_profile() -> settings:
    """The profile's view variant (conftest.py): a request per example is slow, so they run fewer examples."""
    return settings.get_profile(f"{os.environ.get('HYPOTHESIS_PROFILE', 'ci')}-views")


def any_request_is_answered_safely(case: ViewCase, world: World) -> None:
    """Any request the row's URL pattern can get passes check_answer."""

    @views_profile()
    @given(data=st.data())
    def run(data: st.DataObject) -> None:
        with rolled_back(), case.patches(world) if case.patches else ExitStack():
            drawn = data.draw(requests(case, world), label="request")
            check_answer(case, drawn, send(world, drawn))

    run()


def echo_sweep(case: ViewCase, world: World) -> None:
    """Every query and POST field the row declares, set to markup and template syntax at once (query fields on every
    request, POST fields where build sends them), on each of its URL names with the first valid value for each path
    parameter: each of the row's methods as each role allowed in (pages, and what a POST re-renders, differ by role).
    check_answer then fails on any field echoed raw.

    Deterministic, so CI checks every declared field every run: the random search rarely pairs an allowed role, the
    method a field is read on, and markup in that one field (the cotton attrs XSS passed ci-views)."""
    probe = ECHO_PROBE + TEMPLATE_PROBE
    query = {name: [probe] for name in (case.query(world) if case.query else {})}
    fields = dict.fromkeys(case.post(world) if case.post else {}, probe)
    for name in (case.name, *case.also):
        kwargs = {param: world.ids(kind)[0] for _, param, kind in route_params(case, name)}
        for role in sorted(case.allowed):
            for method in case.methods:
                drawn = build(case, role=role, method=method, name=name, kwargs=kwargs, query=query, fields=fields)
                with rolled_back(), case.patches(world) if case.patches else ExitStack():
                    check_answer(case, drawn, send(world, drawn))


def every_allowed_role_gets_in(case: ViewCase, world: World) -> None:
    """Each role the row allows gets past the view's permission check on some real object: a GET (a POST with no
    fields where the view takes only POST) is not refused for at least one valid path. A row that lists a role its
    view refuses is too wide, and its oracle never checks that role's refusal."""
    if case.finds_none:
        return
    for name in case.gate_names or (case.name, *case.also):
        params = route_params(case, name)
        count = max((len(world.ids(kind)) for _, _, kind in params), default=1)
        for role in sorted(case.allowed):
            answers = []
            for index in range(count):
                kwargs = {param: (ids := world.ids(kind))[index % len(ids)] for _, param, kind in params}
                for method in ("GET", "POST"):
                    if method not in case.methods:
                        continue
                    query = case.entry_query(world) if case.entry_query else {}
                    drawn = build(case, role=role, method=method, name=name, kwargs=kwargs, query=query, fields={})
                    with rolled_back(), case.patches(world) if case.patches else ExitStack():
                        answer = send(world, drawn)
                        shape = unknown_refusal(answer)
                    answers.append((drawn.path, method, answer.response.status_code, *([shape] if shape else [])))
                    if answer.response.status_code != 405:
                        break
                # An answer in refusal words the classifier doesn't know (a POST's error redirect, a warning, a page
                # saying no) keeps the visitor out of this object too
                if not refuses_visitor(answer, method) and not shape:
                    break
            else:
                raise AssertionError(f"{name}: {role} is allowed by the row but refused everywhere: {answers}")


def snapshot(models_: tuple[type[models.Model], ...]) -> dict[str, list[dict[str, Any]]]:
    """The rows of these models, without the columns that record when they were saved."""
    skipped = {"updated_at", "modified_at", "last_updated"}
    return {
        model._meta.label: [
            {k: v for k, v in row.items() if k not in skipped} for row in model.objects.order_by("pk").values()
        ]
        for model in models_
    }


def repeated_post_changes_nothing_more(case: ViewCase, world: World) -> None:
    """The case's POST, sent a second time, leaves its models' rows as the first left them."""

    @views_profile()
    @given(data=st.data())
    def run(data: st.DataObject) -> None:
        with rolled_back(), case.patches(world) if case.patches else ExitStack():
            drawn = data.draw(requests(case, world, role=case.idempotent_as, method="POST"), label="request")
            check_answer(case, drawn, send(world, drawn))
            once = snapshot(case.idempotent)
            check_answer(case, drawn, send(world, drawn))
            assert snapshot(case.idempotent) == once, f"{drawn.path}: posting again changed the rows"

    run()
