"""A team can file an incident report up to its limit per window: the affected IP is optional, as the model has it,
and editable, so a report without boxes still goes in; past the limit it is refused, staff filing for a team aren't."""

import re
import threading
from datetime import timedelta

import psycopg2
import pytest
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from scoring.models import IncidentReport
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(blue_team_user: User, mock_quotient_client: object) -> Client:
    client = Client()
    client.force_login(blue_team_user)
    return client


def _report(**fields: str) -> dict[str, str]:
    return {
        "source_ip": "10.0.0.5",
        "attack_detected_at": "2026-01-01T10:00",
        "attack_description": "A shell on the web host",
        **fields,
    }


@pytest.mark.parametrize("destination_ip", ["10.0.1.20", ""])
def test_a_report_without_boxes_goes_in(client: Client, blue_team: Team, destination_ip: str) -> None:
    response = client.post(
        reverse("scoring:submit_incident_report"),
        _report(destination_ip=destination_ip, autofilled_ip="192.0.2.20"),
    )

    incident = IncidentReport.objects.get(team=blue_team)
    assert response.status_code == 302
    assert incident.destination_ip == (destination_ip or None)
    assert incident.affected_boxes == []


def test_the_autofill_marker_has_a_length_limit(client: Client) -> None:
    response = client.post(reverse("scoring:submit_incident_report"), _report(autofilled_ip="x" * 40))

    assert response.status_code == 200
    assert response.context["form"].has_error("autofilled_ip", "max_length")
    assert not IncidentReport.objects.exists()


def test_the_affected_ip_can_be_typed_and_is_not_marked_required(client: Client, blue_team: Team) -> None:
    html = client.get(reverse("scoring:submit_incident_report")).content.decode()

    ip_input = re.search(r'<input[^>]*name="destination_ip"[^>]*>', html)
    assert ip_input
    assert "readonly" not in ip_input.group(0)
    assert not re.search(r'<label for="id_destination_ip"\s+class="required"', html)


def _filed(team: Team, user: User, count: int, *, ago: timedelta = timedelta()) -> None:
    for _ in range(count):
        report = IncidentReport.objects.create(
            team=team,
            submitted_by=user,
            attack_description="Seen",
            source_ip="10.0.0.5",
            attack_detected_at=timezone.now(),
        )
        IncidentReport.objects.filter(pk=report.pk).update(created_at=timezone.now() - ago)


def test_a_team_at_its_submission_limit_is_refused(client: Client, blue_team: Team, blue_team_user: User) -> None:
    _filed(blue_team, blue_team_user, IncidentReport.SUBMISSION_LIMIT)

    response = client.post(reverse("scoring:submit_incident_report"), _report(), follow=True)

    assert IncidentReport.objects.filter(team=blue_team).count() == IncidentReport.SUBMISSION_LIMIT
    assert f"Your team has filed {IncidentReport.SUBMISSION_LIMIT} incident reports" in response.content.decode()


def test_reports_older_than_the_window_dont_count(client: Client, blue_team: Team, blue_team_user: User) -> None:
    _filed(blue_team, blue_team_user, IncidentReport.SUBMISSION_LIMIT, ago=IncidentReport.SUBMISSION_WINDOW)
    _filed(blue_team, blue_team_user, IncidentReport.SUBMISSION_LIMIT - 1)

    response = client.post(reverse("scoring:submit_incident_report"), _report())

    assert response.status_code == 302
    assert IncidentReport.objects.filter(team=blue_team).count() == 2 * IncidentReport.SUBMISSION_LIMIT


def test_staff_filing_for_a_team_arent_limited(
    white_team_user: User, blue_team: Team, blue_team_user: User, mock_quotient_client: object
) -> None:
    _filed(blue_team, blue_team_user, IncidentReport.SUBMISSION_LIMIT)
    client = Client()
    client.force_login(white_team_user)

    client.post(reverse("scoring:submit_incident_report"), _report(team=str(blue_team.pk)))

    assert IncidentReport.objects.filter(team=blue_team).count() == IncidentReport.SUBMISSION_LIMIT + 1


@pytest.mark.django_db(transaction=True)
def test_the_count_waits_for_a_submission_saving_under_the_teams_lock(blue_team: Team) -> None:
    """Submissions at once mustn't all pass: the count waits for one that holds the team's lock."""
    db = connection.settings_dict
    other = psycopg2.connect(
        host=db["HOST"], port=db["PORT"], user=db["USER"], password=db["PASSWORD"], dbname=db["NAME"]
    )
    other.autocommit = True
    key = f"incident_reports:team:{blue_team.pk}"
    other.cursor().execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", [key])

    def count() -> None:
        try:
            with transaction.atomic():
                IncidentReport.over_submission_limit(blue_team.pk)
        finally:
            connection.close()

    worker = threading.Thread(target=count)
    worker.start()
    try:
        worker.join(timeout=2)
        assert worker.is_alive(), "the count ran without waiting for the team's lock"
    finally:
        other.cursor().execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [key])
        other.close()
    worker.join(timeout=30)
    assert not worker.is_alive()
