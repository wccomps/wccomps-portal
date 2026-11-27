"""Race the real roster writers against score sync using separate PostgreSQL sessions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event as Barrier
from unittest.mock import patch

import pytest
from django.db import connection, connections, transaction
from django.test import Client
from django.urls import reverse
from quotient.client import QuotientClient, ServiceExportEntry, TeamServiceExport
from scoring.models import ServiceDetail, ServiceScore
from scoring.quotient_sync import check_sync, sync_service_scores

from core.services.competition import run_lock
from registration.locks import SCHOOL_LIST_LOCK_ID, school_list_lock
from registration.models import Event, RegistrationContact, RegistrationEventEnrollment, Season, TeamRegistration
from registration.services import ImportBlockedError, SchoolListRow, add_to_school_list, set_team_school
from team.models import Team

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def roster(monkeypatch, gold_team_user):
    season = Season.objects.create(name="Synthetic", year=2031)
    event = Event.objects.create(season=season, name="Exercise", date=date(2031, 1, 1), is_active=True)
    team = Team.objects.create(team_number=3, team_name="Team 03", is_active=False)
    registration = TeamRegistration.objects.create(school_name="Example College", status="approved")
    RegistrationContact.objects.create(registration=registration, name="Example", email="c@example.edu", role="captain")
    RegistrationEventEnrollment.objects.create(event=event, registration=registration)
    client = Client()
    client.force_login(gold_team_user)
    monkeypatch.setattr(QuotientClient, "get_service_export", lambda _: _export())
    monkeypatch.setattr(QuotientClient, "get_uptimes", lambda _: [])
    return event, team, client


def _export():
    return [TeamServiceExport("team03", 3, [ServiceExportEntry("web", 40, 0, 0)], 40, 0, 40, 91)]


def _claim(method, event, client):
    if method == "csv":
        add_to_school_list(event, [SchoolListRow(3, "Example College", "c@example.edu")], "gold")
        return None
    return client.post(reverse("registration_assign_teams", args=[event.pk]), follow=True)


def _sync_on_own_connection():
    try:
        return sync_service_scores()
    finally:
        connections.close_all()


@pytest.mark.parametrize("method", ["csv", "random"])
def test_first_score_commit_excludes_slot_from_automatic_claim(roster, method):
    event, team, client = roster
    ready, resume = Barrier(), Barrier()

    def pause_before_score_write(*args):
        check_sync(*args)
        ready.set()
        assert resume.wait(10)

    with patch("scoring.quotient_sync.check_sync", pause_before_score_write), ThreadPoolExecutor(1) as pool:
        sync = pool.submit(_sync_on_own_connection)
        try:
            assert ready.wait(10)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])
                assert cursor.fetchone() == (False,)
                resume.set()
                school_list_lock()  # Wait for the paused importer to commit before evaluating eligibility.
                if method == "csv":
                    with pytest.raises(ImportBlockedError, match="Team 3 is no longer free"):
                        _claim(method, event, client)
                else:
                    response = _claim(method, event, client)
                    assert response is not None and b"Not enough teams available" in response.content
        finally:
            resume.set()
        assert sync.result(timeout=10)["teams_created"] == 1
    team.refresh_from_db()
    assert not team.is_active and not event.team_assignments.exists()
    assert ServiceScore.objects.get(team=team).service_points == ServiceDetail.objects.get(team=team).points == 40


@pytest.mark.parametrize("change", ["csv", "random", "event", "school", "replaced_slot", "unchanged"])
def test_import_refuses_changed_original_slot_or_event_after_fetch_started(roster, change):
    event, team, client = roster
    if change in ("school", "unchanged"):
        set_team_school(team, "Original College", contact_email="o@example.edu")
    ready, resume = Barrier(), Barrier()

    def pause_fetch(_client):
        ready.set()
        assert resume.wait(10)
        return _export()

    with patch.object(QuotientClient, "get_service_export", pause_fetch), ThreadPoolExecutor(1) as pool:
        sync = pool.submit(_sync_on_own_connection)
        try:
            assert ready.wait(10)
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [SCHOOL_LIST_LOCK_ID])
                assert cursor.fetchone() == (True,), "Quotient I/O must not hold the roster lock"
                if change in ("csv", "random"):
                    _claim(change, event, client)
                elif change == "school":
                    set_team_school(team, "Another College", contact_email="a@example.edu")
                elif change == "replaced_slot":
                    Team.objects.filter(pk=team.pk).delete()
                    Team.objects.create(team_number=3, team_name="Replacement", is_active=False)
                elif change == "event":
                    event.close()
                    Event.objects.create(season=event.season, name="Later", date=event.date, is_active=True)
        finally:
            resume.set()
        if change == "unchanged":
            assert sync.result(timeout=10)["teams_created"] == 1
        else:
            with pytest.raises(ValueError, match="active event changed|team slot changed"):
                sync.result(timeout=10)
            assert not ServiceScore.objects.exists() and not ServiceDetail.objects.exists()
    if change in ("csv", "random"):
        team.refresh_from_db()
        assert team.is_active and event.team_assignments.get().school.name == "Example College"


def test_import_can_take_roster_lock_inside_stops_run_lock(roster):
    event, team, _ = roster
    set_team_school(team, "Original College", contact_email="o@example.edu")
    with run_lock() as locked:
        assert locked and sync_service_scores()["teams_created"] == 1
    assert ServiceScore.objects.get(team=team).service_points == 40
    assert event.team_assignments.get().school.name == "Original College"
