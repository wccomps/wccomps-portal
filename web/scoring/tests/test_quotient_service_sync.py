"""Service sync matches Quotient's export and uptime series to portal teams by the number in the team name."""

import httpx
import pytest
from django.core.cache import cache
from quotient import client as client_module

from scoring.models import ServiceDetail, ServiceScore
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
UPTIMES = {
    "series": [
        {"Name": "team01", "Data": [{"Service": "web", "Uptime": 0.1}]},
        {"Name": "team03", "Data": [{"Service": "web", "Uptime": 0.9}]},
    ]
}


@pytest.fixture(autouse=True)
def _quotient(settings, monkeypatch):
    settings.QUOTIENT_API_URL = "https://scoring.example"
    settings.QUOTIENT_USERNAME = "admin"
    settings.QUOTIENT_PASSWORD = "secret"
    cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/engine/export/scores":
            return httpx.Response(200, json=EXPORT)
        if request.url.path == "/api/graphs/uptimes":
            return httpx.Response(200, json=UPTIMES)
        return httpx.Response(200, json={})

    real_client = httpx.Client
    monkeypatch.setattr(client_module.httpx, "Client", lambda: real_client(transport=httpx.MockTransport(handler)))
    yield
    cache.clear()


@pytest.mark.django_db
def test_sync_attaches_scores_and_uptimes_to_the_named_team() -> None:
    Team.objects.create(team_number=1, team_name="One")
    team3 = Team.objects.create(team_number=3, team_name="Three")

    result = sync_service_scores()

    assert result["total"] == 1
    assert ServiceScore.objects.get().team == team3
    detail = ServiceDetail.objects.get()
    assert detail.team == team3
    assert float(detail.uptime) == pytest.approx(0.9)
    assert detail.sla_violations == 1


@pytest.mark.django_db
def test_sync_derives_service_max() -> None:
    from decimal import Decimal

    from scoring.models import ScoringTemplate

    Team.objects.create(team_number=3, team_name="Three")
    ScoringTemplate.objects.create(
        service_weight=Decimal("40"),
        inject_weight=Decimal("40"),
        orange_weight=Decimal("20"),
        service_max=Decimal("1"),
    )

    sync_service_scores()

    assert ScoringTemplate.objects.first().service_max == Decimal("45")


@pytest.mark.django_db
def test_sync_keeps_service_max_when_quotient_exports_no_max_points(monkeypatch) -> None:
    """A Quotient that predates max_points still syncs scores; the maximum stays as it was."""
    from decimal import Decimal

    from scoring.models import ScoringTemplate

    Team.objects.create(team_number=3, team_name="Three")
    ScoringTemplate.objects.create(service_max=Decimal("777"))
    monkeypatch.delitem(EXPORT[0]["services"][0], "max_points")

    assert sync_service_scores()["total"] == 1

    assert ScoringTemplate.objects.get().service_max == Decimal("777")


@pytest.mark.django_db
def test_a_maximum_too_large_to_store_refuses_the_sync(monkeypatch) -> None:
    from decimal import Decimal

    from scoring.models import ScoringTemplate

    Team.objects.create(team_number=3, team_name="Three")
    ScoringTemplate.objects.create(service_max=Decimal("777"))
    monkeypatch.setitem(EXPORT[0]["services"][0], "max_points", 10**30)

    with pytest.raises(ValueError, match="the service maximum"):
        sync_service_scores()

    assert not ServiceScore.objects.exists()
    assert ScoringTemplate.objects.get().service_max == Decimal("777")


@pytest.mark.django_db
def test_a_second_sync_fetches_fresh_scores(monkeypatch) -> None:
    """Sync is an explicit fetch: the client's 5-minute cache must not hand back the previous numbers."""
    team3 = Team.objects.create(team_number=3, team_name="Three")
    sync_service_scores()
    monkeypatch.setitem(EXPORT[0], "total_points", 80)
    monkeypatch.setitem(EXPORT[0]["services"][0], "service_points", 85)

    sync_service_scores()

    assert ServiceDetail.objects.get(team=team3).points == 85


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
