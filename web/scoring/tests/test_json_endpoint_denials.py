"""JSON endpoints answer unauthorized callers with a 403 JSON body, not a login-page redirect."""

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("name", "args", "method"),
    [
        ("scoring:api_scores", [], "get"),
        ("orange_team:assignment_save", [1], "post"),
    ],
)
def test_denied_with_json_403(blue_team_user, name, args, method):
    client = Client()
    client.force_login(blue_team_user)
    response = getattr(client, method)(reverse(name, args=args))
    assert response.status_code == 403
    assert response.json() == {"error": "Access denied"}
