"""Every refusal and missing record is answered one way (core.errors): signed out, sent to sign in; signed in without
the role, a 403; a record that is missing or another team's, a 404 that reads the same either way; input a view
refuses, a 400. A page gets the error page and a JSON endpoint (or a fetch) gets {"error": ...} with the same
status."""

import re
from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from scoring.models import IncidentReport

from core.tests.roles import role_user
from team.models import Team
from ticketing.models import Ticket, TicketAttachment, TicketCategory

pytestmark = pytest.mark.django_db

FETCH = {"Sec-Fetch-Mode": "cors"}


def signed_in(role: str) -> Client:
    client = Client()
    client.force_login(role_user(role))
    return client


def ticket(team: Team, number: str) -> Ticket:
    return Ticket.objects.create(
        team=team, category=TicketCategory.objects.get(pk=6), ticket_number=number, title=f"Ticket {number}"
    )


@pytest.fixture
def other_team() -> Team:
    return Team.objects.create(team_number=2, team_name="Team 02")


def body(response: Any) -> str:
    """The answer without its CSRF tokens, which differ on every page."""
    return re.sub(
        r'(name="csrf-token" content=|name="csrfmiddlewaretoken" value=)"[^"]*"', r"\1", response.content.decode()
    )


def is_error_page(response: Any, status: int) -> bool:
    return response.status_code == status and "error.html" in [t.name for t in response.templates]


# Signed out ------------------------------------------------------------------------------------------------------


def test_signed_out_page_is_sent_to_sign_in_with_next() -> None:
    response = Client().get(reverse("admin_teams"))
    assert response.status_code == 302
    assert response["Location"] == "/auth/login/?next=%2Fops%2Fadmin%2Fteams%2F"


# Signed in, role not allowed ---------------------------------------------------------------------------------------


def test_signed_in_without_the_role_gets_the_403_page_with_a_link_home() -> None:
    response = signed_in("blue_team").get(reverse("admin_teams"))
    assert is_error_page(response, 403)
    page = response.content.decode()
    assert "have access" in page
    assert f'href="{reverse("ticket_list")}"' in page  # the blue team's landing page
    assert not list(response.context["messages"])


def test_red_team_landing_link_on_the_403_page() -> None:
    response = signed_in("red_team").get(reverse("school_info"))
    assert is_error_page(response, 403)
    assert f'href="{reverse("scoring:submit_red_score")}"' in response.content.decode()


def test_incident_list_refuses_a_role_with_no_team_with_a_403() -> None:
    response = signed_in("red_team").get(reverse("scoring:incident_list"))
    assert is_error_page(response, 403)


# Another team's record ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("url_name", ["ticket_detail", "ticket_detail_dynamic"])
def test_another_teams_ticket_answers_404_like_a_missing_one(url_name: str, other_team: Team) -> None:
    client = signed_in("blue_team")
    ticket(other_team, "T002-001")
    theirs = client.get(reverse(url_name, args=["T002-001"]))
    missing = client.get(reverse(url_name, args=["T002-999"]))
    assert theirs.status_code == missing.status_code == 404
    assert body(theirs) == body(missing)


@pytest.mark.parametrize("url_name", ["ticket_comment", "ticket_cancel", "ticket_attachment_upload"])
def test_another_teams_ticket_answers_404_to_a_post(url_name: str, other_team: Team) -> None:
    client = signed_in("blue_team")
    ticket(other_team, "T002-001")
    theirs = client.post(reverse(url_name, args=["T002-001"]), {"comment": "hi"})
    missing = client.post(reverse(url_name, args=["T002-999"]), {"comment": "hi"})
    assert theirs.status_code == missing.status_code == 404
    assert body(theirs) == body(missing)
    assert not Ticket.objects.get(ticket_number="T002-001").comments.exists()


def test_another_teams_attachment_answers_404(other_team: Team) -> None:
    client = signed_in("blue_team")
    attachment = TicketAttachment.objects.create(
        ticket=ticket(other_team, "T002-001"), file_data=b"x", filename="a.txt", mime_type="text/plain"
    )
    theirs = client.get(reverse("ticket_attachment_download", args=["T002-001", attachment.pk]))
    missing = client.get(reverse("ticket_attachment_download", args=["T002-001", attachment.pk + 1000]))
    assert theirs.status_code == missing.status_code == 404
    assert body(theirs) == body(missing)


def test_another_teams_incident_report_answers_404(other_team: Team) -> None:
    client = signed_in("blue_team")
    incident = IncidentReport.objects.create(
        team=other_team,
        submitted_by=role_user("gold_team"),
        source_ip="10.0.0.9",
        attack_detected_at=timezone.now(),
        attack_description="Seen",
        affected_boxes=["web"],
    )
    for method, url_name in (("get", "scoring:view_incident_report"), ("post", "scoring:delete_incident_report")):
        send = getattr(client, method)
        theirs = send(reverse(url_name, args=[incident.pk]))
        missing = send(reverse(url_name, args=[incident.pk + 1000]))
        assert theirs.status_code == missing.status_code == 404, url_name
        assert body(theirs) == body(missing)
    assert IncidentReport.objects.filter(pk=incident.pk).exists()


def test_staff_see_every_teams_ticket(other_team: Team) -> None:
    ticket(other_team, "T002-001")
    response = signed_in("ticketing_support").get(reverse("ticket_detail", args=["T002-001"]))
    assert response.status_code == 200


# JSON endpoints ----------------------------------------------------------------------------------------------------


def test_json_endpoint_answers_403_as_json_without_a_fetch_header() -> None:
    response = signed_in("blue_team").post(reverse("admin_team_action", args=[1]), {"action": "activate"})
    assert response.status_code == 403
    assert isinstance(response.json()["error"], str)


def test_json_endpoint_answers_404_as_json() -> None:
    response = signed_in("admin").post(reverse("admin_team_action", args=[99]), {"action": "activate"})
    assert response.status_code == 404
    assert response.json()["error"]


def test_json_endpoint_answers_405_as_json() -> None:
    response = signed_in("admin").get(reverse("admin_team_action", args=[1]))
    assert response.status_code == 405
    assert response.json()["error"]
    assert response["Allow"] == "POST"


def test_json_endpoint_answers_400_as_json_with_the_views_reason() -> None:
    Team.objects.get_or_create(team_number=1, defaults={"team_name": "Team 01"})
    response = signed_in("admin").post(reverse("admin_team_action", args=[1]), {"action": "nope"})
    assert response.status_code == 400
    assert response.json() == {"error": "Unknown action"}


def test_a_fetch_to_a_page_gets_json_errors() -> None:
    client = signed_in("blue_team")
    assert client.get(reverse("admin_teams"), headers=FETCH).json()["error"]
    assert client.get("/no/such/page/", headers=FETCH).status_code == 404
    assert client.get("/no/such/page/", headers=FETCH).json()["error"]


def test_htmx_gets_the_page_not_json() -> None:
    response = signed_in("blue_team").get(reverse("admin_teams"), headers={**FETCH, "HX-Request": "true"})
    assert is_error_page(response, 403)


# Missing pages and bad input ---------------------------------------------------------------------------------------


def test_missing_page_is_the_error_page() -> None:
    response = signed_in("blue_team").get("/no/such/page/")
    assert is_error_page(response, 404)


def test_a_page_405_is_the_error_page() -> None:
    response = signed_in("blue_team").get(reverse("ticket_cancel", args=["T001-001"]))
    assert is_error_page(response, 405)
    assert response["Allow"] == "POST"


def test_bad_input_is_a_400_page_with_the_reason() -> None:
    client = signed_in("blue_team")
    ticket(Team.objects.get(team_number=1), "T001-001")
    response = client.post(reverse("ticket_attachment_upload", args=["T001-001"]))
    assert is_error_page(response, 400)
    assert "No file provided" in response.content.decode()


def test_bad_input_to_a_fetch_is_a_400_json() -> None:
    client = signed_in("blue_team")
    ticket(Team.objects.get(team_number=1), "T001-001")
    response = client.post(reverse("ticket_attachment_upload", args=["T001-001"]), headers=FETCH)
    assert response.status_code == 400
    assert response.json() == {"error": "No file provided"}


def test_a_server_error_answers_a_fetch_with_json() -> None:
    client = signed_in("admin")
    client.raise_request_exception = False
    with patch("scoring.views.api.get_leaderboard", side_effect=RuntimeError("boom")):
        response = client.get(reverse("scoring:api_scores"))
    assert response.status_code == 500
    assert response.json()["error"]
    assert "boom" not in response.content.decode()
