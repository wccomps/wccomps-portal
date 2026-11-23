"""
Tests for OAuth flow with mocked Authentik responses.

These tests verify the full OAuth callback logic without requiring
a real Authentik server.
"""

import base64
import json
import re
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.http import HttpResponse
from django.test import Client
from django.utils.html import escape

from core.models import CredentialsReset, UserGroups
from core.tests.external_json import Answer, serving

OAUTH_CONFIG = {
    "client_id": "test-client-id",
    "client_secret": "test-secret",
    "authorization_endpoint": "https://auth.example.com/authorize/",
    "token_endpoint": "https://auth.example.com/token/",
    "userinfo_endpoint": "https://auth.example.com/userinfo/",
    "end_session_endpoint": "https://auth.example.com/end-session/",
}
GOOD_TOKEN = {"access_token": "test-token"}
GOOD_USERINFO = {"sub": "refused-sub", "preferred_username": "refused", "groups": []}


def _json(body: object, status: int = 200) -> Answer:
    return Answer(status, json.dumps(body).encode())


GOOD_ANSWERS = (_json(GOOD_TOKEN), _json(GOOD_USERINFO))


def _start_sign_in(client: Client, next_url: str | None = None) -> str:
    """Begin a login in this browser, coming back to next_url; the signed state Authentik hands back."""
    with patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG):
        response = client.get("/auth/login/", {} if next_url is None else {"next": next_url})
    assert response.status_code == 302
    return parse_qs(urlparse(response.url).query)["state"][0]


def _callback(client: Client, query: dict[str, str], *answers: Answer) -> HttpResponse:
    """The callback, with Authentik's token and userinfo requests getting these answers in turn."""
    with patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG), serving(list(answers)):
        return client.get("/auth/callback/", query)


def _login(client: Client, state: str, userinfo: object, *, id_token: str | None = None) -> HttpResponse:
    """The callback for this state, Authentik answering with an access token (and this ID token) and this userinfo."""
    tokens = GOOD_TOKEN | ({"id_token": id_token} if id_token else {})
    return _callback(client, {"code": "test-code", "state": state}, _json(tokens), _json(userinfo))


def _id_token(auth_time: float) -> str:
    """An ID token whose claims say the person signed in to Authentik at auth_time (the signature isn't read)."""
    payload = base64.urlsafe_b64encode(json.dumps({"auth_time": int(auth_time)}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _shown(response) -> tuple[int, str, str]:
    """The error page's status, the title its browser tab shows before the site's name, and its message. The site's
    name comes from a context processor, so it shows only on a page rendered with its request."""
    title = re.search(r"<title>\s*(.*?)\s*</title>", response.content.decode(), re.S)
    assert title, response.content
    site = f" - {escape(settings.SITE_NAME)}"
    assert title.group(1).endswith(site), title.group(1)
    return response.status_code, title.group(1).removesuffix(site), response.context["error_message"]


@pytest.fixture
def oauth_state_session(client: Client) -> tuple[Client, str]:
    """A browser that has begun a login, and the state it was handed."""
    return client, _start_sign_in(client)


@pytest.mark.django_db
class TestOAuthLogin:
    """Test OAuth login initiation."""

    def test_login_redirects_to_authentik(self):
        """Login should redirect to Authentik authorization endpoint."""
        client = Client()
        with patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG):
            response = client.get("/auth/login/")

        assert response.status_code == 302
        assert "auth.example.com/authorize" in response.url
        assert "client_id=test-client-id" in response.url
        assert "scope=openid" in response.url

    def test_login_includes_signed_state_in_redirect(self):
        """Login should include a signed state parameter in the redirect URL."""
        state = _start_sign_in(Client())
        state_data = signing.loads(state, max_age=300)
        assert "n" in state_data
        assert len(state_data["n"]) > 10

    def test_login_encodes_next_url_in_state(self):
        """Login should encode next URL in the signed state parameter."""
        state = _start_sign_in(Client(), "/dashboard/")
        state_data = signing.loads(state, max_age=300)
        assert state_data["next"] == "/dashboard/"

    def test_login_without_client_id_shows_error(self):
        """Login without client ID configured should show error."""
        client = Client()
        with patch("core.oauth._get_oauth_config", return_value={**OAUTH_CONFIG, "client_id": "", "client_secret": ""}):
            response = client.get("/auth/login/")

        assert response.status_code == 500
        assert b"Configuration Error" in response.content

    def test_login_without_oidc_url_shows_error(self, settings):
        """AUTHENTIK_OIDC_URL defaults to empty without AUTHENTIK_URL: no redirect to a relative /authorize/."""
        settings.AUTHENTIK_CLIENT_ID = "test-client-id"
        settings.AUTHENTIK_OIDC_URL = ""

        response = Client().get("/auth/login/")

        assert response.status_code == 500
        assert b"Configuration Error" in response.content


@pytest.mark.django_db
class TestOAuthCallback:
    """Test OAuth callback handling."""

    def test_callback_updates_existing_user_groups(self, oauth_state_session):
        """Callback should update groups for existing user."""
        client, state = oauth_state_session

        user = User.objects.create_user(username="existinguser", email="existing@example.com")
        UserGroups.objects.create(
            user=user,
            authentik_id="existing-authentik-id",
            groups=["OldGroup"],
        )

        response = _login(
            client,
            state,
            {
                "sub": "existing-authentik-id",
                "preferred_username": "existinguser",
                "email": "existing@example.com",
                "groups": ["WCComps_GoldTeam", "NewGroup"],
            },
        )

        assert response.status_code == 302

        user_groups = UserGroups.objects.get(user=user)
        assert "WCComps_GoldTeam" in user_groups.groups
        assert "NewGroup" in user_groups.groups
        assert "OldGroup" not in user_groups.groups

    def test_callback_handles_username_change(self, oauth_state_session):
        """Callback should update username if changed in Authentik."""
        client, state = oauth_state_session

        user = User.objects.create_user(username="oldusername")
        UserGroups.objects.create(
            user=user,
            authentik_id="user-authentik-id",
            groups=["SomeGroup"],
        )

        response = _login(
            client, state, {"sub": "user-authentik-id", "preferred_username": "newusername", "groups": ["SomeGroup"]}
        )

        assert response.status_code == 302

        user.refresh_from_db()
        assert user.username == "newusername"

    @pytest.mark.parametrize("before", [True, False])
    def test_a_sign_in_from_before_a_credentials_reset_is_refused(self, oauth_state_session, before):
        """An authorization code issued before the reset must not undo its portal sign-out."""
        from datetime import UTC, datetime

        client, state = oauth_state_session
        user = User.objects.create(username="team06")
        reset_at = time.time() - 60
        UserGroups.objects.create(user=user, authentik_id="sub-06", groups=[])
        CredentialsReset.objects.create(authentik_id="sub-06", reset_at=datetime.fromtimestamp(reset_at, UTC))
        auth_time = reset_at - 10 if before else reset_at + 10

        response = _login(
            client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=_id_token(auth_time)
        )

        assert ("_auth_user_id" in client.session) is not before
        if before:
            assert _shown(response)[:2] == (200, "Signed Out")
            assert b"password was reset" in response.content

    def test_without_auth_time_the_sign_in_counts_from_when_it_began(self, oauth_state_session):
        """The login page signed when it started; a reset after that is later than this sign-in."""
        from datetime import timedelta

        from django.utils import timezone

        client, state = oauth_state_session
        user = User.objects.create(username="team06")
        reset_at = timezone.now() + timedelta(seconds=10)
        UserGroups.objects.create(user=user, authentik_id="sub-06", groups=[])
        CredentialsReset.objects.create(authentik_id="sub-06", reset_at=reset_at)

        _login(client, state, {"sub": "sub-06", "preferred_username": "team06"})

        assert "_auth_user_id" not in client.session

    @pytest.mark.parametrize("auth_time", ["1" + "0" * 400, "NaN", "Infinity", "-Infinity"])
    def test_an_auth_time_that_is_no_moment_counts_from_when_the_sign_in_began(self, oauth_state_session, auth_time):
        from datetime import timedelta

        from django.utils import timezone

        client, state = oauth_state_session
        CredentialsReset.objects.create(authentik_id="sub-06", reset_at=timezone.now() + timedelta(seconds=10))
        payload = base64.urlsafe_b64encode(f'{{"auth_time": {auth_time}}}'.encode()).decode().rstrip("=")

        response = _login(
            client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=f"header.{payload}.signature"
        )

        assert response.status_code == 200
        assert "_auth_user_id" not in client.session

    def test_signing_in_marks_a_disabled_account_enabled(self, oauth_state_session):
        """Authentik signs in only an enabled account, so the next refresh needn't wait to restore permissions."""
        client, state = oauth_state_session
        user = User.objects.create(username="team05")
        UserGroups.objects.create(user=user, authentik_id="sub-05", groups=[], account_active=False)

        _login(client, state, {"sub": "sub-05", "preferred_username": "team05", "groups": ["WCComps_GoldTeam"]})

        assert UserGroups.objects.get(user=user).permission_groups == ["WCComps_GoldTeam"]

    def test_reused_username_gets_a_fresh_account_not_the_old_one(self, oauth_state_session):
        """Identity is the Authentik sub: a new identity with an old username inherits nothing."""
        from team.models import DiscordLink

        client, state = oauth_state_session
        old = User.objects.create_user(username="reused")
        UserGroups.objects.create(user=old, authentik_id="old-authentik-id", groups=["WCComps_Discord_Admin"])
        DiscordLink.objects.create(discord_id=111, discord_username="old-discord", user=old, is_active=True)

        response = _login(
            client, state, {"sub": "new-authentik-id", "preferred_username": "reused", "groups": ["WCComps_GoldTeam"]}
        )

        assert response.status_code == 302
        new = User.objects.get(username="reused")
        assert new.pk != old.pk
        assert int(client.session["_auth_user_id"]) == new.pk
        assert UserGroups.objects.get(user=new).authentik_id == "new-authentik-id"
        assert not DiscordLink.objects.filter(user=new).exists()

        old.refresh_from_db()
        assert old.username == f"reused~replaced-{old.pk}"
        # Still tied to its own sub, so the periodic refresh decides its groups
        assert UserGroups.objects.get(user=old).authentik_id == "old-authentik-id"
        assert DiscordLink.objects.get(discord_id=111).user == old

    def test_reused_username_does_not_inherit_django_flags(self, oauth_state_session):
        """A pre-existing account without an Authentik identity (e.g. createsuperuser) isn't reused either."""
        client, state = oauth_state_session
        old = User.objects.create_superuser(username="admin", password="unused-test-password")  # noqa: S106

        _login(client, state, {"sub": "someone-new", "preferred_username": "admin", "groups": []})

        new = User.objects.get(username="admin")
        assert new.pk != old.pk
        assert not new.is_superuser
        assert not new.is_staff

    def test_a_reused_username_as_long_as_the_column_moves_its_holder_aside_within_it(self, oauth_state_session):
        """The holder's new name keeps as much of the old one as fits beside the suffix."""
        client, state = oauth_state_session
        name = "n" * User._meta.get_field("username").max_length
        old = User.objects.create_user(username=name)
        UserGroups.objects.create(user=old, authentik_id="old-sub", groups=[])

        assert _login(client, state, {"sub": "new-sub", "preferred_username": name}).status_code == 302

        old.refresh_from_db()
        suffix = f"~replaced-{old.pk}"
        assert old.username == name[: len(name) - len(suffix)] + suffix
        assert User.objects.get(usergroups__authentik_id="new-sub").username == name

    def test_renamed_user_takes_their_new_name_from_a_stale_holder(self, oauth_state_session):
        """Portal username follows Authentik's: /link looks the Authentik user up by it."""
        client, state = oauth_state_session
        me = User.objects.create_user(username="old-name")
        UserGroups.objects.create(user=me, authentik_id="my-id", groups=[])
        stale = User.objects.create_user(username="new-name")
        UserGroups.objects.create(user=stale, authentik_id="deleted-id", groups=["WCComps_GoldTeam"])

        _login(client, state, {"sub": "my-id", "preferred_username": "new-name", "groups": []})

        me.refresh_from_db()
        stale.refresh_from_db()
        assert me.username == "new-name"
        assert stale.username == f"new-name~replaced-{stale.pk}"
        assert UserGroups.objects.get(user=stale).authentik_id == "deleted-id"

    def test_a_sign_in_begun_at_the_front_page_lands_on_the_roles_own_page(self, oauth_state_session, settings):
        """A login with no page to return to (its next is /) goes to the role's landing page."""
        from django.urls import reverse

        client, state = oauth_state_session

        response = _login(
            client, state, {"sub": "w", "preferred_username": "w", "groups": [settings.PORTAL_WHITE_GROUP]}
        )

        assert response.status_code == 302
        assert response.url == reverse("leaderboard_page")

    def test_moved_aside_account_takes_its_current_name_on_login(self, oauth_state_session):
        """An account whose name was taken, but whose identity was only renamed, logs in normally."""
        client, state = oauth_state_session
        user = User.objects.create_user(username="carol~replaced-1")
        UserGroups.objects.create(user=user, authentik_id="carol-id", groups=[])

        response = _login(client, state, {"sub": "carol-id", "preferred_username": "dave", "groups": ["G"]})

        assert response.status_code == 302
        user.refresh_from_db()
        assert user.username == "dave"
        assert client.get("/").wsgi_request.user.pk == user.pk

    def test_callback_redirects_to_next_url(self, client):
        """Callback should redirect to next URL encoded in the signed state."""
        state = _start_sign_in(client, "/my-dashboard/")

        response = _login(
            client, state, {"sub": "redirect-test-id", "preferred_username": "redirectuser", "groups": []}
        )

        assert response.status_code == 302
        assert response.url == "/my-dashboard/"

    def test_callback_preserves_next_url_with_query_params(self, client):
        """Callback should preserve next URL containing query parameters (e.g. link token)."""
        next_url = "/auth/link-callback?token=abc123"
        state = _start_sign_in(client, next_url)

        response = _login(client, state, {"sub": "link-test-id", "preferred_username": "linkuser", "groups": []})

        assert response.status_code == 302
        assert response.url == next_url

    def test_callback_rejects_state_from_another_browser(self, oauth_state_session):
        """Login CSRF: a valid state (and code) replayed in a different browser logs nobody in, even one that has
        begun a sign-in of its own."""
        _client, state = oauth_state_session
        victim = Client()
        _start_sign_in(victim)

        response = _login(victim, state, {"sub": "attacker-sub", "preferred_username": "attacker", "groups": []})

        assert _shown(response) == (200, "Session Expired", "Please try logging in again.")
        assert "_auth_user_id" not in victim.session
        assert not User.objects.filter(username="attacker").exists()

    def test_state_is_single_use(self, oauth_state_session):
        client, state = oauth_state_session
        userinfo = {"sub": "once-sub", "preferred_username": "once", "groups": []}

        assert _login(client, state, userinfo).status_code == 302
        assert b"Session Expired" in _login(client, state, userinfo).content

    def test_callback_rejects_external_next_url(self):
        """Callback should reject next URLs pointing to external hosts."""

        client = Client()
        session = client.session
        session["oauth_nonce"] = "test-nonce"
        session.save()

        # oauth_login drops an external next before signing it, so only a forged state carries one
        state = signing.dumps({"n": "test-nonce", "next": "https://evil.com/steal"})

        response = _login(client, state, {"sub": "redirect-test-id-2", "preferred_username": "safeuser", "groups": []})

        assert response.status_code == 302
        assert "evil.com" not in response.url


@pytest.mark.django_db
class TestEachRefusal:
    """A sign-in refused before an account is looked at answers the error page with its title and message, and signs
    nobody in."""

    @pytest.mark.parametrize(
        ("query", "title", "message"),
        [
            ({"error": "access_denied"}, "Login Cancelled", "You cancelled the login."),
            ({"error": "server_error", "error_description": "down"}, "Login Failed", "Please try again."),
            ({"code": "test-code"}, "Session Expired", "Please try again."),
            ({"code": "test-code", "state": "forged"}, "Security Error", "Please try again."),
        ],
    )
    def test_a_refusal_before_the_state_is_verified(self, client, query, title, message):
        response = client.get("/auth/callback/", query)

        assert _shown(response) == (200, title, message)
        assert "_auth_user_id" not in client.session

    def test_an_expired_state(self, client):
        with patch("django.core.signing.time.time", return_value=time.time() - 600):
            state = signing.dumps({"n": "test-nonce", "next": "/"})

        response = client.get("/auth/callback/", {"code": "test-code", "state": state})

        assert _shown(response) == (200, "Session Expired", "Please try again.")
        assert "_auth_user_id" not in client.session

    @pytest.mark.parametrize(
        ("query", "answers", "title"),
        [
            ({}, GOOD_ANSWERS, "Login Failed"),
            ({"code": "test-code"}, (_json(GOOD_TOKEN, 502), _json(GOOD_USERINFO)), "Authentication Failed"),
            ({"code": "test-code"}, (_json({"access_token": ""}), _json(GOOD_USERINFO)), "Authentication Failed"),
            ({"code": "test-code"}, (_json({"access_token": 7}), _json(GOOD_USERINFO)), "Authentication Failed"),
            ({"code": "test-code"}, (_json(GOOD_TOKEN), _json(GOOD_USERINFO, 502)), "Authentication Failed"),
        ],
        ids=["no code", "token request fails", "empty access token", "access token not text", "userinfo fails"],
    )
    def test_a_refusal_after_the_state_is_verified(self, oauth_state_session, query, answers, title):
        """No code is a failed login; a token or userinfo request that fails, or no access token, a failed
        authentication. Each row's answers would sign in but for its one fault: a 502 is refused whatever its body."""
        client, state = oauth_state_session

        response = _callback(client, {**query, "state": state}, *answers)

        assert _shown(response) == (200, title, "Please try again.")
        assert "_auth_user_id" not in client.session
        assert not UserGroups.objects.exists()

    @pytest.mark.parametrize("failing", ["token", "userinfo"])
    def test_a_failed_request_to_authentik_is_logged_with_its_error(self, oauth_state_session, caplog, failing):
        client, state = oauth_state_session
        answers = (_json(GOOD_TOKEN, 502),) if failing == "token" else (_json(GOOD_TOKEN), _json(GOOD_USERINFO, 502))

        with caplog.at_level("ERROR", logger="core.oauth"):
            _callback(client, {"code": "test-code", "state": state}, *answers)

        (logged,) = [r.getMessage() for r in caplog.records if r.name == "core.oauth"]
        assert "502 Bad Gateway" in logged
        assert OAUTH_CONFIG[f"{failing}_endpoint"] in logged

    def test_authentiks_error_and_its_reason_are_logged(self, client, caplog):
        with caplog.at_level("WARNING", logger="core.oauth"):
            client.get("/auth/callback/", {"error": "server_error", "error_description": "the database is down"})

        assert [r.getMessage() for r in caplog.records if r.name == "core.oauth"] == [
            "OAuth error: server_error - the database is down"
        ]


@pytest.mark.django_db
def test_the_code_is_redeemed_and_the_claims_read_as_authentik_expects(oauth_state_session):
    """The token request carries the grant, the code, the client's credentials and the redirect URI the login sent;
    the userinfo request carries the access token the exchange answered. Both give up after the configured timeout,
    so a hung Authentik can't hold the sign-in open."""
    client, state = oauth_state_session

    with (
        patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG),
        serving(list(GOOD_ANSWERS)) as sent,
    ):
        response = client.get("/auth/callback/", {"code": "the-code", "state": state})

    assert response.status_code == 302
    token, userinfo = sent
    assert (token.method, str(token.url)) == ("POST", OAUTH_CONFIG["token_endpoint"])
    assert parse_qs(token.content.decode()) == {
        "grant_type": ["authorization_code"],
        "client_id": ["test-client-id"],
        "client_secret": ["test-secret"],
        "code": ["the-code"],
        "redirect_uri": ["http://testserver/auth/callback/"],
    }
    assert (userinfo.method, str(userinfo.url)) == ("GET", OAUTH_CONFIG["userinfo_endpoint"])
    assert userinfo.headers["Authorization"] == "Bearer test-token"
    assert UserGroups.objects.get().authentik_id == GOOD_USERINFO["sub"]
    timeout = settings.HTTPX_DEFAULT_TIMEOUT
    assert (
        token.extensions["timeout"]
        == userinfo.extensions["timeout"]
        == dict.fromkeys(("connect", "read", "write", "pool"), timeout)
    )


SUB = "s" * 64  # UserGroups.authentik_id's column
EMAIL = "e" * 242 + "@example.edu"  # User.email's 254
NAME = "n" * 150  # User.username's


@pytest.mark.django_db
class TestClaims:
    """The account a sign-in makes from Authentik's userinfo claims, or its refusal when they can't make one."""

    @pytest.mark.parametrize(
        ("userinfo", "account"),
        [
            (
                {"sub": "a", "preferred_username": "alice", "email": "alice@example.edu", "groups": ["G1", "G2"]},
                ("a", "alice", "alice@example.edu", ["G1", "G2"]),
            ),
            ({"sub": "a", "email": "alice@example.edu"}, ("a", "alice@example.edu", "alice@example.edu", [])),
            (
                {"sub": "a", "preferred_username": "", "email": "x@example.edu"},
                ("a", "x@example.edu", "x@example.edu", []),
            ),
            ({"sub": "a", "preferred_username": "alice", "email": None}, ("a", "alice", "", [])),
            ({"sub": SUB, "preferred_username": NAME, "email": EMAIL}, (SUB, NAME, EMAIL, [])),
        ],
    )
    def test_an_account_from_the_claims(self, oauth_state_session, userinfo, account):
        """The username is the email when there is none; no email is an empty one, no groups none; each claim may
        fill its column."""
        client, state = oauth_state_session

        response = _login(client, state, userinfo)

        assert response.status_code == 302
        row = UserGroups.objects.select_related("user").get()
        assert (row.authentik_id, row.user.username, row.user.email, row.groups) == account
        assert int(client.session["_auth_user_id"]) == row.user.pk

    @pytest.mark.parametrize(
        ("userinfo", "reason"),
        [
            ({"preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": "", "preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": 7, "preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": SUB + "s", "preferred_username": "alice"}, "no usable sub claim"),
            ({"sub": "a\x00", "preferred_username": "alice"}, "a NUL character"),
            ({"sub": "a"}, "no usable username"),
            ({"sub": "a", "preferred_username": 7}, "no usable username"),
            ({"sub": "a", "preferred_username": "al\x00ice"}, "no usable username"),
            ({"sub": "a", "preferred_username": NAME + "n"}, "no usable username"),
            ({"sub": "a", "preferred_username": "alice", "email": 7}, "an unusable email"),
            ({"sub": "a", "preferred_username": "alice", "email": "e" + EMAIL}, "an unusable email"),
            ({"sub": "a", "preferred_username": "alice", "email": "alice\x00@example.edu"}, "a NUL character"),
            ({"sub": "a", "preferred_username": "alice", "groups": "G1"}, "groups that aren't a list of names"),
            ({"sub": "a", "preferred_username": "alice", "groups": None}, "groups that aren't a list of names"),
            ({"sub": "a", "preferred_username": "alice", "groups": ["G1", 7]}, "groups that aren't a list of names"),
            ({"sub": "a", "preferred_username": "alice", "groups": ["G1", "G\x00"]}, "a NUL character"),
        ],
    )
    def test_claims_that_make_no_account_are_refused(self, oauth_state_session, caplog, userinfo, reason):
        """A missing, empty or non-text sub or username, a NUL, a claim past its column, or groups that aren't a
        list of names. The page only says the data was invalid; the log says which claim, for whoever fixes the
        scope mapping."""
        client, state = oauth_state_session

        with caplog.at_level("ERROR", logger="core.oauth"):
            response = _login(client, state, userinfo)

        assert _shown(response) == (200, "Authentication Failed", "Invalid user data.")
        assert not UserGroups.objects.exists()
        assert "_auth_user_id" not in client.session
        assert [r.getMessage() for r in caplog.records if r.name == "core.oauth"] == [f"Userinfo unusable: {reason}"]


@pytest.mark.django_db
class TestOAuthLogout:
    """Test OAuth logout."""

    def test_logout_clears_session(self):
        """Logout should clear Django session."""
        client = Client()
        user = User.objects.create_user(username="logoutuser")
        client.force_login(user)

        # Verify logged in
        assert "_auth_user_id" in client.session

        with patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG):
            client.post("/auth/logout/")

        # Verify logged out
        assert "_auth_user_id" not in client.session

    def test_logout_redirects_to_authentik(self):
        """Logout should redirect to Authentik end-session endpoint."""
        client = Client()
        user = User.objects.create_user(username="logoutuser2")
        client.force_login(user)

        with patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG):
            response = client.post("/auth/logout/")

        assert response.status_code == 302
        assert "auth.example.com/end-session" in response.url

    def test_a_get_leaves_the_user_signed_in(self):
        """A link another site plants (or an image) can't sign anyone out."""
        client = Client()
        client.force_login(User.objects.create_user(username="logoutuser3"))

        response = client.get("/auth/logout/")

        assert response.status_code == 405
        assert "_auth_user_id" in client.session

    def test_a_post_without_the_csrf_token_leaves_the_user_signed_in(self):
        """Another site's form posts without the token."""
        client = Client(enforce_csrf_checks=True)
        client.force_login(User.objects.create_user(username="logoutuser4"))

        response = client.post("/auth/logout/")

        assert response.status_code == 403
        assert "_auth_user_id" in client.session
