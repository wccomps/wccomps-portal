"""Team folder mode (settings.TEAM_ACCOUNT_PATH): a region whose Authentik has no team groups keeps its team accounts in
one user folder. These run against a fake Authentik that, like the real one with an unknown filter, answers every user
whatever path is asked for, so each row's path has to be checked."""

import json
from collections.abc import Iterator
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client
from django.utils import timezone

from core import team_accounts
from core.admin_views.readiness import (
    CAN_CHANGE_USERNAME,
    _check_blueteam_bindings,
    _check_sub_mode,
    _check_team_accounts_exist,
    _check_usernames_fixed,
    checks,
)
from core.authentik_manager import AuthentikAPIError, AuthentikManager, blueteam_binding
from core.models import CompetitionConfig, UserGroups
from core.services.competition import NO_FOLDER_ACCOUNT, run_competition
from core.services.user_groups import refresh_user_groups
from core.team_accounts import FOLDER_CACHE_SECONDS, _FolderCache, accounts_by_team, blueteam_kinds
from core.tests.test_oauth_flow import _login, _shown, _start_sign_in
from core.utils import drain
from team.models import Team

pytestmark = pytest.mark.django_db

FOLDER = "blue-users"


def _user(pk: int, username: str, path: str = FOLDER, *, uid: str | None = None) -> dict[str, object]:
    return {
        "pk": pk,
        "username": username,
        "uid": uid or f"uid-{username}",
        "is_active": False,
        "groups": [],
        "path": path,
    }


USERS = [
    _user(11, "team01a"),
    _user(12, "team01b"),
    _user(13, "team01c", "users"),  # outside the folder
    _user(21, "team02a"),
    _user(31, "team03a"),
    _user(99, "bond007"),  # in the folder, matching no team
    _user(100, "team1x"),  # in the folder, not the full pattern
]


class FakeAuthentik:
    """Users, one controlled application and its bindings, the system settings and the portal's OAuth2 provider;
    records every write and every user listing page served."""

    def __init__(self, users: list[dict[str, object]], bindings: list[dict[str, object]] | None = None) -> None:
        self.users = users
        self.bindings = bindings or []
        self.groups: list[dict[str, object]] = []
        self.writes: list[tuple[str, str, object]] = []
        self.listed_pages: list[int] = []
        self.page_size = 100
        self.fail = False
        self.system_settings: dict[str, object] = {"default_user_change_username": False, "avatars": "initials"}
        self.providers = [{"pk": 1, "client_id": "portal-client", "sub_mode": "hashed_user_id"}]

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503, json={"detail": "down"})
        path = request.url.path
        params = parse_qs(request.url.query.decode())
        if request.method != "GET":
            body = json.loads(request.content) if request.content else None
            self.writes.append((request.method, path, body))
            if path.startswith("/api/v3/core/users/") and isinstance(body, dict) and "is_active" in body:
                pk = int(path.rstrip("/").rsplit("/", 1)[1])
                next(u for u in self.users if u["pk"] == pk)["is_active"] = body["is_active"]
            return httpx.Response(200, json={})
        if path == "/api/v3/core/users/":
            # path= ignored, as Authentik ignores a filter it doesn't know; username= honoured (find_user checks it too)
            users = [u for u in self.users if "username" not in params or u["username"] == params["username"][0]]
            page = int(params.get("page", ["1"])[0])
            self.listed_pages.append(page)
            chunk = users[(page - 1) * self.page_size : page * self.page_size]
            more = page * self.page_size < len(users)
            return httpx.Response(200, json={"results": chunk, "pagination": {"next": page + 1 if more else 0}})
        if path == "/api/v3/core/groups/":
            return httpx.Response(200, json={"results": self.groups, "pagination": {"next": 0}})
        if path == "/api/v3/core/applications/vpn/":
            return httpx.Response(200, json={"pk": "app-1", "slug": "vpn"})
        if path == "/api/v3/policies/bindings/":
            return httpx.Response(200, json={"results": self.bindings, "pagination": {"next": 0}})
        if path == "/api/v3/admin/settings/":
            return httpx.Response(200, json=self.system_settings)
        if path == "/api/v3/providers/oauth2/":
            # client_id= ignored too: each row's client ID has to be checked
            return httpx.Response(200, json={"results": self.providers, "pagination": {"next": 0}})
        return httpx.Response(404, json={"detail": "Not found"})

    def manager(self) -> AuthentikManager:
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return AuthentikManager(base_url="https://auth.test", api_token="token", client=client)

    def toggled(self, is_active: bool) -> list[str]:
        """The usernames set to this enabled state."""
        patched = [p for m, p, b in self.writes if m == "PATCH" and b == {"is_active": is_active}]
        pks = {int(p.rstrip("/").rsplit("/", 1)[1]) for p in patched}
        return sorted(str(u["username"]) for u in self.users if u["pk"] in pks)


@pytest.fixture(autouse=True)
def _fresh_folder_cache(monkeypatch) -> None:
    monkeypatch.setattr(team_accounts, "_folder_cache", _FolderCache())


@pytest.fixture
def team_folder(settings) -> None:
    settings.TEAM_ACCOUNT_PATH = FOLDER
    settings.TEAM_ACCOUNT_PATTERN = "team{number}[a-i]"
    settings.TEAM_GROUP_PATTERN = "Team-Blue-{number:02d}"
    settings.AUTHENTIK_CLIENT_ID = "portal-client"


@pytest.fixture
def teams(team_folder) -> None:
    for number in (1, 2, 3):
        Team.objects.create(team_number=number, team_name=f"Team {number}", is_active=number != 3)


def _stored(uid: str, groups: list[str]) -> UserGroups:
    return UserGroups.objects.create(user=User.objects.create(username=f"user-{uid}"), authentik_id=uid, groups=groups)


def _groups(uid: str) -> list[str]:
    return sorted(UserGroups.objects.get(authentik_id=uid).groups)


# ---------------------------------------------------------------------------------------------------------------
# Group refresh
# ---------------------------------------------------------------------------------------------------------------


def test_the_refresh_keeps_a_folder_accounts_team_group(team_folder):
    _stored("uid-team01a", ["Team-Blue-01"])
    _stored("uid-team02a", [])

    refresh_user_groups(FakeAuthentik(USERS).manager())

    assert _groups("uid-team01a") == ["Team-Blue-01"]
    assert _groups("uid-team02a") == ["Team-Blue-02"]


def test_an_account_that_leaves_the_folder_loses_its_team(team_folder):
    _stored("uid-team01a", ["Team-Blue-01"])
    moved = [{**u, "path": "users"} if u["username"] == "team01a" else u for u in USERS]

    refresh_user_groups(FakeAuthentik(moved).manager())

    assert _groups("uid-team01a") == []


@pytest.mark.parametrize("username", ["bond007", "team1x", "team01c"])
def test_an_account_matching_no_team_in_the_folder_gets_none(team_folder, username):
    _stored(f"uid-{username}", ["Team-Blue-00", "Team-Blue-01"])

    refresh_user_groups(FakeAuthentik(USERS).manager())

    assert _groups(f"uid-{username}") == []


def test_with_the_folder_unset_the_refresh_gives_no_team_group(team_folder, settings):
    settings.TEAM_ACCOUNT_PATH = ""
    _stored("uid-team01a", ["Team-Blue-01"])

    refresh_user_groups(FakeAuthentik(USERS).manager())

    assert _groups("uid-team01a") == []


def test_a_user_listing_without_paths_changes_nothing(team_folder):
    """Without the path the refresh can't tell team accounts apart; stripping every team would be worse."""
    _stored("uid-team01a", ["Team-Blue-01"])
    no_paths = [{k: v for k, v in u.items() if k != "path"} for u in USERS]

    with pytest.raises(AuthentikAPIError, match="a result without"):
        refresh_user_groups(FakeAuthentik(no_paths).manager())

    assert _groups("uid-team01a") == ["Team-Blue-01"]


def test_a_refreshed_folder_account_holds_its_team(teams):
    _stored("uid-team01a", [])
    refresh_user_groups(FakeAuthentik(USERS).manager())

    assert list(Team.objects.held_by(_groups("uid-team01a")).values_list("team_number", flat=True)) == [1]


def test_a_folder_account_holds_its_teams_stored_group(teams):
    """Team.authentik_group decides who holds a team, so a team whose group was renamed keeps its accounts."""
    Team.objects.filter(team_number=2).update(authentik_group="Renamed02")
    _stored("uid-team02a", ["Team-Blue-02"])
    refresh_user_groups(FakeAuthentik(USERS).manager())

    assert _groups("uid-team02a") == ["Renamed02"]
    assert list(Team.objects.held_by(_groups("uid-team02a")).values_list("team_number", flat=True)) == [2]


def test_an_authentik_group_named_like_a_team_grants_nothing_in_the_folder_mode(teams):
    """A real Authentik group with a team's name (left over, or made by someone) is no team: only the folder is."""
    fake = FakeAuthentik([{**u, "groups": ["g1"]} if u["username"] in ("bond007", "team02a") else u for u in USERS])
    fake.groups = [{"pk": "g1", "name": "Team-Blue-01", "parents": []}]
    _stored("uid-bond007", [])
    _stored("uid-team02a", [])
    refresh_user_groups(fake.manager())

    assert _groups("uid-bond007") == []
    assert _groups("uid-team02a") == ["Team-Blue-02"]


# ---------------------------------------------------------------------------------------------------------------
# Start and stop
# ---------------------------------------------------------------------------------------------------------------

POLICY_BINDING = {
    "pk": "b-1",
    "target": "app-1",
    "enabled": False,
    "policy": "p1",
    "policy_obj": {"name": "BlueTeam access"},
}


@pytest.fixture
def authentik(teams) -> Iterator[FakeAuthentik]:
    fake = FakeAuthentik([dict(u) for u in USERS], [dict(POLICY_BINDING)])
    now = timezone.now()
    CompetitionConfig.objects.update_or_create(
        pk=1,
        defaults={
            "controlled_applications": ["vpn"],
            "applications_enabled": False,
            "competition_start_time": now,
            "competition_end_time": now + timedelta(hours=8),
        },
    )
    with (
        patch("core.authentik_manager.AuthentikManager", side_effect=lambda: fake.manager()),
        patch("core.services.user_groups.refresh_user_groups"),
        patch("scoring.quotient_sync.sync_quotient_metadata"),
        patch("scoring.quotient_sync.sync_service_scores"),
    ):
        yield fake


def test_accounts_by_team_lists_only_folder_accounts_matching_the_team(teams):
    accounts = accounts_by_team(FakeAuthentik(USERS).manager(), [1, 2, 3, 4])

    assert {n: a.matched for n, a in accounts.items()} == {
        1: ["team01a", "team01b"],
        2: ["team02a"],
        3: ["team03a"],
        4: [],
    }


def test_a_start_enables_the_active_teams_folder_accounts_and_opens_the_policy_binding(authentik):
    result = drain(run_competition(True, "gold_admin"))

    assert authentik.toggled(True) == ["team01a", "team01b", "team02a"]
    assert ("PUT", "/api/v3/policies/bindings/b-1/", {**POLICY_BINDING, "enabled": True}) in authentik.writes
    assert result.apps_ok == ["vpn"]
    assert result.account_failures == []


def test_a_stop_disables_every_teams_folder_accounts_and_nothing_else(authentik):
    CompetitionConfig.objects.filter(pk=1).update(applications_enabled=True)
    for user in authentik.users:
        user["is_active"] = True
    authentik.bindings[0]["enabled"] = True

    drain(run_competition(False, "gold_admin"))

    assert authentik.toggled(False) == ["team01a", "team01b", "team02a", "team03a"]
    assert ("PUT", "/api/v3/policies/bindings/b-1/", {**POLICY_BINDING, "enabled": False}) in authentik.writes


def test_a_start_fails_an_active_team_with_no_folder_account(authentik):
    authentik.users[:] = [u for u in authentik.users if u["username"] != "team02a"]

    result = drain(run_competition(True, "gold_admin"))

    assert result.account_failures == [("team 02", NO_FOLDER_ACCOUNT)]


# ---------------------------------------------------------------------------------------------------------------
# App gating
# ---------------------------------------------------------------------------------------------------------------


GROUP_BINDING = {
    "pk": "b-2",
    "target": "app-1",
    "enabled": True,
    "group": "g1",
    "group_obj": {"name": "Portal_BlueTeam"},
}
STAFF_BINDING = {"pk": "b-3", "target": "app-1", "enabled": True, "group": "g9", "group_obj": {"name": "Staff"}}


def _binding(fake: FakeAuthentik) -> dict[str, object] | None:
    return blueteam_binding(fake.manager().app_bindings("app-1"), blueteam_kinds())


def test_a_policy_binding_on_another_application_is_not_taken(team_folder):
    other = {**POLICY_BINDING, "target": "app-2"}
    assert _binding(FakeAuthentik([], [other])) is None


def test_a_policy_binding_counts_only_in_team_folder_mode(settings):
    """Without the folder a policy named like the blue team (a "deny-blueteam" policy) is no gate to toggle."""
    fake = FakeAuthentik([], [dict(POLICY_BINDING), dict(GROUP_BINDING)])

    settings.TEAM_ACCOUNT_PATH = ""
    assert _binding(fake) == GROUP_BINDING
    fake.bindings = [dict(POLICY_BINDING)]
    assert _binding(fake) is None
    settings.TEAM_ACCOUNT_PATH = FOLDER
    assert _binding(fake) == POLICY_BINDING


def test_the_folder_mode_prefers_the_policy_binding_to_a_group_one(team_folder):
    """An app keeping a leftover blue team group binding beside the policy that lets the folder in: a start and stop
    toggle the policy."""
    assert _binding(FakeAuthentik([], [dict(GROUP_BINDING), dict(POLICY_BINDING)])) == POLICY_BINDING


@pytest.mark.parametrize("folder", ["", FOLDER])
def test_a_group_binding_is_found_in_either_mode(settings, folder):
    settings.TEAM_ACCOUNT_PATH = folder
    unrelated = {"pk": "b-3", "target": "app-1", "enabled": True, "policy": "p2", "policy_obj": {"name": "Staff"}}
    binding = _binding(FakeAuthentik([], [unrelated, dict(GROUP_BINDING)]))
    assert binding is not None and binding["pk"] == "b-2"


@pytest.fixture
def controlled_vpn() -> None:
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"controlled_applications": ["vpn"]})


def test_readiness_fails_a_lone_blue_team_binding_in_the_folder_mode(team_folder, controlled_vpn):
    """With the blue team binding the app's only enabled one, a stop disabling it lets every Authentik user in."""
    fake = FakeAuthentik([], [dict(POLICY_BINDING), {**STAFF_BINDING, "enabled": False}])
    with _readiness(fake):
        severity, detail, _ = _check_blueteam_bindings()
        assert severity == "fail" and "vpn" in detail and "second binding" in detail
        fake.bindings[1]["enabled"] = True
        assert _check_blueteam_bindings()[0] == "pass"


@pytest.mark.parametrize(
    "extra",
    [GROUP_BINDING, {**POLICY_BINDING, "pk": "b-2"}],
    ids=["a policy and a group", "two policies"],
)
def test_readiness_fails_more_than_one_blue_team_binding_in_the_folder_mode(team_folder, controlled_vpn, extra):
    """A start and stop toggle only the preferred binding, so another would stay as it was; nor is another blue team
    binding the app's second binding."""
    fake = FakeAuthentik([], [dict(POLICY_BINDING), dict(extra), dict(STAFF_BINDING)])
    with _readiness(fake):
        severity, detail, _ = _check_blueteam_bindings()

    assert severity == "fail" and "vpn (extra b-2)" in detail and "remove the extra ones" in detail


def test_a_lone_blue_team_group_binding_passes_without_the_folder(controlled_vpn):
    with _readiness(FakeAuthentik([], [dict(GROUP_BINDING)])):
        assert _check_blueteam_bindings()[0] == "pass"


# ---------------------------------------------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------------------------------------------


def _sign_in(fake: FakeAuthentik, userinfo: dict[str, object], *, expect: int = 302) -> HttpResponse:
    """A full sign-in in a fresh browser, with the region's scope mapping's userinfo."""
    client = Client()
    state = _start_sign_in(client)
    with patch("core.oauth.AuthentikManager", side_effect=lambda: fake.manager()):
        response = _login(client, state, userinfo)
    assert response.status_code == expect
    return response


def _claimed(username: str) -> dict[str, object]:
    """What the region's scope mapping sends for a folder account: the name without its letter, and the team group."""
    return {"sub": f"uid-{username}", "preferred_username": username[:-1], "groups": [f"Team-Blue-{username[-3:-1]}"]}


def test_two_accounts_of_one_team_keep_their_own_usernames(teams):
    fake = FakeAuthentik(USERS)

    for _ in range(2):
        _sign_in(fake, _claimed("team01a"))
        _sign_in(fake, _claimed("team01b"))

    accounts = {g.authentik_id: g.user.username for g in UserGroups.objects.select_related("user")}
    assert accounts == {"uid-team01a": "team01a", "uid-team01b": "team01b"}
    assert _groups("uid-team01a") == _groups("uid-team01b") == ["Team-Blue-01"]


def test_a_sign_in_gives_a_folder_account_its_team_group_without_the_claim(teams):
    _sign_in(FakeAuthentik(USERS), {"sub": "uid-team02a", "preferred_username": "team02"})

    assert User.objects.get(usergroups__authentik_id="uid-team02a").username == "team02a"
    assert _groups("uid-team02a") == ["Team-Blue-02"]


def test_an_account_outside_the_folder_signs_in_as_claimed(teams):
    _sign_in(FakeAuthentik(USERS), {"sub": "uid-team01c", "preferred_username": "someone"})

    assert User.objects.get(usergroups__authentik_id="uid-team01c").username == "someone"
    assert _groups("uid-team01c") == []


@pytest.mark.parametrize("authentik_username", ["team01" + "d" * 200, "team01\x00d"], ids=["too long", "NUL"])
def test_a_folder_account_whose_authentik_username_cant_be_one_signs_in_as_claimed(teams, caplog, authentik_username):
    """A folder account named past the username column, or with a NUL, keeps the claimed name, and it is logged."""
    fake = FakeAuthentik([*USERS, _user(41, authentik_username, uid="uid-odd")])

    _sign_in(fake, {"sub": "uid-odd", "preferred_username": "team01"})

    assert User.objects.get(usergroups__authentik_id="uid-odd").username == "team01"
    assert "unusable Authentik username" in caplog.text


@pytest.mark.parametrize(
    ("username", "held"),
    [
        ("team01c", []),  # outside the folder
        ("bond007", []),  # in it, matching no team
        ("team02a", ["Team-Blue-02"]),  # its own team's group, not the claimed one
        ("staff", []),  # not in the folder at all
    ],
)
def test_a_claimed_team_group_is_never_taken_at_sign_in(teams, username, held):
    """The scope mapping may claim a team's group for anyone; only the folder gives one, at sign-in too."""
    claim = {"sub": f"uid-{username}", "preferred_username": username, "groups": ["Team-Blue-01", "Portal_GoldTeam"]}
    _sign_in(FakeAuthentik(USERS), claim)

    assert _groups(f"uid-{username}") == sorted(["Portal_GoldTeam", *held])


def test_when_authentik_cant_be_asked_the_sign_in_is_refused(teams, caplog):
    """Signing in as claimed would let team03a and team03b rename each other, the thing the lookup prevents."""
    fake = FakeAuthentik(USERS)
    fake.fail = True

    response = _sign_in(fake, _claimed("team01a"), expect=503)

    assert _shown(response) == (503, "Sign-in Unavailable", "Try again in a moment.")
    assert not UserGroups.objects.exists()
    assert "team folder lookup failed" in caplog.text


def test_when_authentik_cant_be_asked_a_staff_sign_in_goes_ahead(teams, settings, caplog):
    """Staff and admins sign in while the folder can't be read: their claim names a staff role's group."""
    fake = FakeAuthentik(USERS)
    fake.fail = True
    claim = [settings.PORTAL_GOLD_GROUP, "Team-Blue-01"]

    _sign_in(fake, {"sub": "uid-staff", "preferred_username": "gold-admin", "groups": claim})

    assert User.objects.get(usergroups__authentik_id="uid-staff").username == "gold-admin"
    assert _groups("uid-staff") == [settings.PORTAL_GOLD_GROUP]
    assert "Team folder lookup failed" in caplog.text


def test_when_authentik_cant_be_asked_a_folder_account_claiming_no_group_is_refused(teams):
    """A region whose scope mapping makes up no team group: the account would sign in under the stripped name."""
    fake = FakeAuthentik(USERS)
    fake.fail = True

    _sign_in(fake, {"sub": "uid-team01a", "preferred_username": "team01"}, expect=503)

    assert not UserGroups.objects.exists()


def test_sign_ins_share_the_folder_read_for_a_minute(teams):
    fake = FakeAuthentik(USERS)
    fake.page_size = 2

    _sign_in(fake, _claimed("team01b"))
    assert fake.listed_pages == [1]  # stopped at the page holding team01b
    _sign_in(fake, _claimed("team01a"))
    _sign_in(fake, _claimed("team01b"))
    assert fake.listed_pages == [1]

    _sign_in(fake, _claimed("team02a"))  # on page 2, not read yet
    assert fake.listed_pages == [1, 1, 2]

    cache = team_accounts._folder_cache
    cache.accounts = {u: (user, at - FOLDER_CACHE_SECONDS) for u, (user, at) in cache.accounts.items()}
    _sign_in(fake, _claimed("team01a"))
    assert fake.listed_pages == [1, 1, 2, 1]


def test_a_sign_in_outside_the_folder_lists_it_once_a_minute(teams):
    fake = FakeAuthentik(USERS)
    fake.page_size = 2
    staff = {"sub": "uid-staff", "preferred_username": "gold-admin", "groups": ["Portal_GoldTeam"]}

    _sign_in(fake, staff)
    _sign_in(fake, staff)

    assert fake.listed_pages == [1, 2, 3, 4]
    assert User.objects.get(usergroups__authentik_id="uid-staff").username == "gold-admin"


def test_with_the_folder_unset_a_sign_in_asks_authentik_nothing(teams, settings):
    settings.TEAM_ACCOUNT_PATH = ""
    fake = FakeAuthentik(USERS)

    with patch.object(FakeAuthentik, "handle", side_effect=AssertionError("asked Authentik")):
        _sign_in(fake, _claimed("team01a"))

    assert User.objects.get(usergroups__authentik_id="uid-team01a").username == "team01"


# ---------------------------------------------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------------------------------------------


def test_readiness_lists_an_active_team_without_a_folder_account(teams):
    fake = FakeAuthentik([u for u in USERS if u["username"] != "team02a"])
    with patch("core.admin_views.readiness.AuthentikManager", side_effect=lambda: fake.manager()):
        severity, detail, _ = _check_team_accounts_exist()

    assert severity == "fail"
    assert "team 02" in detail and FOLDER in detail and "team 01" not in detail


def _readiness(fake: FakeAuthentik):
    return patch("core.admin_views.readiness.AuthentikManager", side_effect=lambda: fake.manager())


def test_readiness_fails_while_users_may_change_their_username(team_folder):
    fake = FakeAuthentik([])
    with _readiness(fake):
        assert _check_usernames_fixed()[0] == "pass"
        fake.system_settings["default_user_change_username"] = True
        severity, detail, _ = _check_usernames_fixed()
        assert severity == "fail" and "rename itself" in detail
        fake.system_settings = {"avatars": "initials"}
        assert _check_usernames_fixed()[0] == "fail"
        fake.fail = True
        severity, detail, _ = _check_usernames_fixed()

    assert severity == "fail" and "view System settings" in detail


@pytest.mark.parametrize("on", ["account", "group", "parent group"])
def test_readiness_fails_a_folder_account_whose_attributes_let_it_rename_itself(team_folder, on):
    """Authentik's default user settings flow reads the attribute on the user and its groups before the system
    setting."""
    fake = FakeAuthentik([{**_user(11, "team01a"), "groups": ["g2"]}, _user(21, "team02a")])
    fake.groups = [{"pk": "g1", "name": "Parent", "parents": []}, {"pk": "g2", "name": "Child", "parents": ["g1"]}]
    with _readiness(fake):
        assert _check_usernames_fixed()[0] == "pass"
        if on == "account":
            fake.users[0]["attributes"] = {CAN_CHANGE_USERNAME: True}
        else:
            fake.groups[0 if on == "parent group" else 1]["attributes"] = {CAN_CHANGE_USERNAME: True}
        severity, detail, _ = _check_usernames_fixed()

    assert severity == "fail" and "team01a" in detail and "team02a" not in detail


def test_readiness_fails_unless_the_portals_provider_uses_the_hashed_user_id(team_folder):
    fake = FakeAuthentik([])
    with _readiness(fake):
        assert _check_sub_mode()[0] == "pass"
        fake.providers = [{"pk": 1, "client_id": "portal-client", "sub_mode": "user_username"}]
        severity, detail, _ = _check_sub_mode()
        assert severity == "fail" and "user_username" in detail
        # Another provider's mode doesn't count, as when Authentik ignores the client_id filter
        fake.providers = [{"pk": 2, "client_id": "other-app", "sub_mode": "hashed_user_id"}]
        severity, detail, _ = _check_sub_mode()

    assert severity == "fail" and "sees no OAuth2 provider" in detail


def test_the_team_folder_checks_run_only_in_team_folder_mode(team_folder, settings):
    names = [name for name, _ in checks()]
    assert {"Usernames fixed", "Sign-in subject mode"} <= set(names) and "Group membership correct" not in names
    settings.TEAM_ACCOUNT_PATH = ""
    assert not {"Usernames fixed", "Sign-in subject mode", "Group membership correct"} & {name for name, _ in checks()}
    settings.TEAM_ACCOUNT_PATTERN = "team{number}"
    names = [name for name, _ in checks()]
    assert names.index("Group membership correct") == names.index("Team accounts exist") + 1
