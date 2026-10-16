"""An event that is open, finalized, or holds assignments or results is season history: it can't be deleted, nor can
its season. Only this check guards open and assigned events; for finalized ones it explains what the PROTECT on
results and archives would refuse."""

from datetime import date

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from registration.models import Event, EventResult, EventTeamAssignment, School, Season
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def season():
    return Season.objects.create(name="2026", year=2026, is_active=True)


@pytest.fixture
def gold_client(gold_team_user):
    client = Client()
    client.force_login(gold_team_user)
    return client


def _event(season, **fields):
    return Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3), **fields)


def _assigned(season):
    event = _event(season)
    team = Team.objects.create(team_number=1, team_name="Team 01")
    EventTeamAssignment.objects.create(event=event, team=team, school=School.for_name("Example University"))
    return event


def _with_result(season):
    event = _event(season)
    EventResult.objects.create(
        event=event, school=School.for_name("Example University"), team_number=1, total_score=0, scorecard={}
    )
    return event


KEPT = {
    "open": (lambda season: _event(season, is_active=True), "is open"),
    "finalized": (lambda season: _event(season, is_finalized=True), "has finalized results"),
    "results": (_with_result, "has finalized results"),
    "assignments": (_assigned, "has schools assigned to teams"),
}


@pytest.mark.parametrize("kind", KEPT)
def test_a_kept_event_is_not_deleted_and_says_why(gold_client, season, kind):
    make, reason = KEPT[kind]
    event = make(season)
    url = reverse("registration_event_delete", args=[event.pk])

    page = gold_client.get(url).content.decode()
    response = gold_client.post(url)

    assert reason in page
    assert Event.objects.filter(pk=event.pk).exists()
    assert response.status_code == 302
    assert any(reason in str(m) for m in get_messages(response.wsgi_request))


def test_an_unused_event_is_deleted(gold_client, season):
    event = _event(season)

    gold_client.post(reverse("registration_event_delete", args=[event.pk]))

    assert not Event.objects.filter(pk=event.pk).exists()


@pytest.mark.parametrize("kind", KEPT)
def test_a_season_holding_a_kept_event_is_not_deleted_and_says_why(gold_client, season, kind):
    make, reason = KEPT[kind]
    make(season)
    url = reverse("registration_season_delete", args=[season.pk])

    page = gold_client.get(url).content.decode()
    response = gold_client.post(url)

    assert reason in page
    assert Season.objects.filter(pk=season.pk).exists()
    assert response.status_code == 302
    assert any(reason in str(m) for m in get_messages(response.wsgi_request))


def test_a_season_with_only_unused_events_is_deleted(gold_client, season):
    _event(season)

    gold_client.post(reverse("registration_season_delete", args=[season.pk]))

    assert not Season.objects.filter(pk=season.pk).exists()
    assert not Event.objects.exists()


@pytest.fixture
def admin_client(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize("kind", KEPT)
def test_the_admin_cannot_delete_a_kept_event_or_its_season(admin_client, season, kind):
    event = KEPT[kind][0](season)

    event_response = admin_client.post(reverse("admin:registration_event_delete", args=[event.pk]), {"post": "yes"})
    season_response = admin_client.post(reverse("admin:registration_season_delete", args=[season.pk]), {"post": "yes"})

    assert event_response.status_code == 403
    assert season_response.status_code == 403
    assert Event.objects.filter(pk=event.pk).exists()


@pytest.mark.parametrize("kind", KEPT)
def test_the_admin_bulk_delete_keeps_a_kept_event_and_its_season(admin_client, season, kind):
    event = KEPT[kind][0](season)

    for model, pk in (("event", event.pk), ("season", season.pk)):
        admin_client.post(
            reverse(f"admin:registration_{model}_changelist"),
            {"action": "delete_selected", "_selected_action": [pk], "post": "yes"},
        )

    assert Event.objects.filter(pk=event.pk).exists()
    assert Season.objects.filter(pk=season.pk).exists()


def test_the_admin_deletes_an_unused_event(admin_client, season):
    event = _event(season)

    admin_client.post(reverse("admin:registration_event_delete", args=[event.pk]), {"post": "yes"})

    assert not Event.objects.filter(pk=event.pk).exists()
