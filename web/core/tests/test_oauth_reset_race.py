"""A sign-in from before a credentials reset can't slip past it: both take the account's lock, and the callback reads
the reset time under it."""

import html
import re
import threading
import time
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth.models import User
from django.core import signing
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from core.authentik_utils import _record_reset, reset_team_credentials
from core.models import CredentialsReset, UserGroups
from core.tests import test_oauth_flow
from core.tests.test_oauth_flow import oauth_state_session  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

_login = test_oauth_flow.TestOAuthCallback._login
_id_token = test_oauth_flow.TestOAuthCallback._id_token


def test_a_reset_committing_while_the_callback_waits_refuses_the_sign_in(oauth_state_session):  # noqa: F811
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[])
    signed_in_at = time.time() - 30
    signed_out = threading.Event()

    def reset_signs_out() -> None:
        try:
            with transaction.atomic():
                _record_reset({"pk": 6, "username": "team06", "uid": "sub-06"}, timezone.now())
                signed_out.set()
                time.sleep(1.5)
        finally:
            connection.close()

    resetting = threading.Thread(target=reset_signs_out)
    resetting.start()
    assert signed_out.wait(10)
    _login(client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=_id_token(signed_in_at))
    resetting.join(10)

    assert not resetting.is_alive()
    assert "_auth_user_id" not in client.session


def test_the_refusal_asks_authentik_to_sign_in_again(oauth_state_session):  # noqa: F811
    """Authentik would otherwise reuse the session from before the reset, and the member would loop."""
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[])
    CredentialsReset.objects.create(authentik_id="sub-06", reset_at=datetime.fromtimestamp(time.time(), UTC))

    page = _login(client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=_id_token(0))

    retry = reverse("oauth_login") + "?prompt=login"
    assert retry.encode() in page.content
    with patch("core.oauth._get_oauth_config") as config:
        config.return_value = {"client_id": "c", "authorization_endpoint": "https://auth.example.com/authorize/"}
        sent = parse_qs(urlparse(client.get(retry).url).query)
    assert sent["prompt"] == ["login"]


def test_a_sign_in_in_the_same_second_as_the_reset_counts_as_after(oauth_state_session):  # noqa: F811
    """auth_time is whole seconds; a sign-in just after a reset at 10.7 s reads as 10 s."""
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    reset_at = time.time() - 30 + 0.7
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[])
    CredentialsReset.objects.create(authentik_id="sub-06", reset_at=datetime.fromtimestamp(reset_at, UTC))

    _login(client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=_id_token(int(reset_at)))

    assert "_auth_user_id" in client.session


def test_a_reset_of_an_account_never_signed_in_to_the_portal_refuses_an_older_sign_in(oauth_state_session):  # noqa: F811
    """The usual state at a reset before a competition: no portal account yet, and a code issued before the reset."""
    client, state = oauth_state_session
    signed_in_at = time.time() - 30
    _reset_team06()

    _login(client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=_id_token(signed_in_at))

    assert "_auth_user_id" not in client.session
    assert not User.objects.filter(username="team06").exists()


def _reset_team06() -> None:
    with patch("core.authentik_utils.AuthentikManager") as manager:
        opened = manager.return_value.__enter__.return_value
        opened.find_user.return_value = {"pk": 6, "username": "team06", "uid": "sub-06"}
        opened.user_sessions.return_value = []
        opened.end_sessions.return_value = None
        reset_team_credentials(6, actor="gold-admin")


def test_a_reset_whose_unlink_failed_still_refuses_an_older_sign_in(oauth_state_session):  # noqa: F811
    client, state = oauth_state_session
    account = User.objects.create(username="team06")
    UserGroups.objects.create(user=account, authentik_id="sub-06", groups=[])
    signed_in_at = time.time() - 30

    with patch("core.authentik_utils.unlink_teams", side_effect=DatabaseError("deadlock detected")):
        _reset_team06()

    assert CredentialsReset.objects.filter(authentik_id="sub-06").exists()
    _login(client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=_id_token(signed_in_at))
    assert "_auth_user_id" not in client.session


def test_the_retry_returns_to_the_page_the_sign_in_began_from(client):
    """A member refused while linking Discord comes back to the link confirmation."""
    config = {
        "client_id": "c",
        "client_secret": "s",
        "authorization_endpoint": "https://auth.example.com/authorize/",
        "token_endpoint": "https://auth.example.com/token/",
        "userinfo_endpoint": "https://auth.example.com/userinfo/",
    }
    link_page = "/auth/link-callback?token=abc"
    with patch("core.oauth._get_oauth_config", return_value=config):
        state = parse_qs(urlparse(client.get("/auth/login/", {"next": link_page}).url).query)["state"][0]
    CredentialsReset.objects.create(authentik_id="sub-06", reset_at=datetime.fromtimestamp(time.time(), UTC))

    page = _login(client, state, {"sub": "sub-06", "preferred_username": "team06"}, id_token=_id_token(0))

    retry = html.unescape(re.search(r'href="([^"]*prompt=login[^"]*)"', page.content.decode()).group(1))
    with patch("core.oauth._get_oauth_config", return_value=config):
        sent = parse_qs(urlparse(client.get(retry).url).query)
    assert sent["prompt"] == ["login"]
    assert signing.loads(sent["state"][0])["next"] == link_page
