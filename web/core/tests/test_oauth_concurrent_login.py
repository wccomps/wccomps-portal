"""Members of a shared team account often log in at the same moment."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client

from core import oauth
from core.models import UserGroups
from core.tests.test_oauth_flow import OAUTH_CONFIG


@pytest.mark.django_db(transaction=True)
def test_simultaneous_first_logins_share_one_account():
    from urllib.parse import parse_qs, urlparse

    http = MagicMock()
    http.__enter__.return_value = http
    http.post.return_value.json.return_value = {"access_token": "t"}
    http.get.return_value.json.return_value = {"sub": "team05-sub", "preferred_username": "team05", "groups": []}
    real_free_username = oauth._free_username

    def slow_free_username(*args, **kwargs):
        time.sleep(0.5)  # widen the window between "no account yet" and creating it
        return real_free_username(*args, **kwargs)

    statuses: list[int] = []

    def start_login() -> tuple[Client, str]:
        client = Client()
        return client, parse_qs(urlparse(client.get("/auth/login/").url).query)["state"][0]

    def log_in(client: Client, state: str) -> None:
        try:
            statuses.append(client.get(f"/auth/callback/?code=c&state={state}").status_code)
        finally:
            connection.close()

    with (
        patch("core.oauth._get_oauth_config", return_value=OAUTH_CONFIG),
        patch("core.oauth.httpx.Client", return_value=http),
        patch("core.oauth._free_username", side_effect=slow_free_username),
    ):
        threads = [threading.Thread(target=log_in, args=start_login()) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert statuses == [302, 302]
    assert User.objects.filter(username="team05").count() == 1
    assert UserGroups.objects.filter(authentik_id="team05-sub").count() == 1
