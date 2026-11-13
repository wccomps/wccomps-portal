"""Service sync matches Quotient's export to portal teams by the number in the team name."""

from decimal import Decimal

import httpx
import pytest
from django.core.cache import cache
from django.urls import reverse
from quotient import client as client_module

from scoring.models import ScoringTemplate, ServiceDetail, ServiceScore
from scoring.quotient_sync import sync_service_scores
from team.models import Team

EXPORT = [
    {
        # Quotient's own team id, unrelated to the portal team number
        "team_id": 1,
        "team_name": "team03",
        "services": [
            {"service_name": "web", "service_points": 40, "max_points": 45, "sla_violations": 1, "sla_penalty": 5}
        ],
        "gross_points": 40,
        "total_sla_penalty": 5,
        "total_points": 35,
    }
]


@pytest.fixture(autouse=True)
def _quotient(settings, monkeypatch):
    settings.QUOTIENT_API_URL = "https://scoring.example"
    settings.QUOTIENT_USERNAME = "admin"
    settings.QUOTIENT_PASSWORD = "secret"
    cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/export/scores":
            return httpx.Response(200, json=EXPORT)
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(client_module.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handler)))
    yield
    cache.clear()


@pytest.mark.django_db
def test_sync_attaches_scores_to_the_named_team() -> None:
    Team.objects.create(team_number=1, team_name="One")
    team3 = Team.objects.create(team_number=3, team_name="Three")

    result = sync_service_scores()

    assert result["total"] == 1
    assert ServiceScore.objects.get().team == team3
    detail = ServiceDetail.objects.get()
    assert detail.team == team3
    assert detail.sla_violations == 1


@pytest.mark.django_db
def test_sync_stores_the_exported_service_max() -> None:
    Team.objects.create(team_number=3, team_name="Three")
    ScoringTemplate.objects.create(service_max=Decimal("1"))

    assert sync_service_scores() == {"total": 1, "service_max_note": None}

    assert ScoringTemplate.objects.get().service_max == Decimal("45")


@pytest.mark.django_db
def test_sync_keeps_service_max_when_the_export_has_no_max_points(monkeypatch) -> None:
    Team.objects.create(team_number=3, team_name="Three")
    ScoringTemplate.objects.create(service_max=Decimal("777"))
    monkeypatch.delitem(EXPORT[0]["services"][0], "max_points")

    result = sync_service_scores()

    assert result["total"] == 1
    assert result["service_max_note"].startswith("The service maximum was not updated")
    assert ScoringTemplate.objects.get().service_max == Decimal("777")


@pytest.mark.django_db
def test_sync_says_when_there_is_no_scoring_setup_to_store_the_maximum_in() -> None:
    Team.objects.create(team_number=3, team_name="Three")

    assert sync_service_scores()["service_max_note"].startswith("The service maximum was not stored")


@pytest.mark.django_db
@pytest.mark.parametrize(("max_points", "why"), [(10**30, "the service maximum"), (39, "Failed to retrieve")])
def test_a_maximum_too_large_or_below_the_points_earned_refuses_the_sync(monkeypatch, max_points, why) -> None:
    Team.objects.create(team_number=3, team_name="Three")
    ScoringTemplate.objects.create(service_max=Decimal("777"))
    monkeypatch.setitem(EXPORT[0]["services"][0], "max_points", max_points)

    with pytest.raises(ValueError, match=why):
        sync_service_scores()

    assert not ServiceScore.objects.exists()
    assert ScoringTemplate.objects.get().service_max == Decimal("777")


@pytest.mark.django_db
@pytest.mark.parametrize("has_inactive_team", [True, False])
def test_only_competing_teams_set_the_service_maximum(monkeypatch, has_inactive_team) -> None:
    """A team Quotient checks that is inactive in the portal, or not in it, would otherwise raise every team's
    maximum (it may have been checked in more rounds)."""
    Team.objects.create(team_number=3, team_name="Three")
    if has_inactive_team:
        Team.objects.create(team_number=48, team_name="Forty-Eight", is_active=False)
    ScoringTemplate.objects.create()
    outsider = {
        "team_name": "team48",
        "services": [{"service_name": "web", "service_points": 90, "max_points": 90}],
        "gross_points": 90,
    }
    monkeypatch.setattr("scoring.tests.test_quotient_service_sync.EXPORT", [*EXPORT, outsider])

    sync_service_scores()

    assert ScoringTemplate.objects.get().service_max == Decimal("45")


@pytest.mark.django_db
@pytest.mark.parametrize("exported", [True, False])
def test_the_sync_page_warns_only_when_the_maximum_was_not_updated(client, gold_team_user, monkeypatch, exported):
    Team.objects.create(team_number=3, team_name="Three")
    ScoringTemplate.objects.create()
    if not exported:
        monkeypatch.delitem(EXPORT[0]["services"][0], "max_points")
    client.force_login(gold_team_user)

    page = client.post(reverse("scoring:sync_scores"), follow=True).content.decode()

    assert "Synced 1 teams" in page
    assert ("The service maximum was not updated" in page) is not exported


@pytest.mark.django_db
def test_a_second_sync_fetches_fresh_scores(monkeypatch) -> None:
    """Sync is an explicit fetch: the client's 5-minute cache must not hand back the previous numbers."""
    team3 = Team.objects.create(team_number=3, team_name="Three")
    sync_service_scores()
    monkeypatch.setitem(EXPORT[0]["services"][0], "service_points", 43)

    sync_service_scores()

    assert ServiceDetail.objects.get(team=team3).points == 43


@pytest.mark.django_db
def test_sync_skips_unscored_teams_with_no_services(monkeypatch) -> None:
    """Teams disabled in Quotient export with 0 points and no services; they don't get empty ServiceScore rows."""
    team3 = Team.objects.create(team_number=3, team_name="Three")
    unplayed = Team.objects.create(team_number=39, team_name="Thirty-Nine", is_active=False)
    ServiceScore.objects.create(team=unplayed)
    monkeypatch.setattr(
        "scoring.tests.test_quotient_service_sync.EXPORT",
        [
            *EXPORT,
            {
                "team_id": 39,
                "team_name": "team39",
                "services": [],
                "gross_points": 0,
                "total_sla_penalty": 0,
                "total_points": 0,
            },
        ],
    )

    result = sync_service_scores()

    assert result["total"] == 1
    assert [s.team for s in ServiceScore.objects.all()] == [team3]
