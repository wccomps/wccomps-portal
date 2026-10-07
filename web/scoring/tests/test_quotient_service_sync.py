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
        "services": [{"service_name": "web", "service_points": 40, "sla_violations": 1, "sla_penalty": 5}],
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

    # web: 40 points at 0.9 uptime -> 44.44 -> 44
    assert ScoringTemplate.objects.first().service_max == Decimal("44")


@pytest.mark.django_db
def test_a_second_sync_fetches_fresh_scores(monkeypatch) -> None:
    """Sync is an explicit fetch: the client's 5-minute cache must not hand back the previous numbers."""
    team3 = Team.objects.create(team_number=3, team_name="Three")
    sync_service_scores()
    monkeypatch.setitem(EXPORT[0], "total_points", 80)
    monkeypatch.setitem(EXPORT[0]["services"][0], "service_points", 85)

    sync_service_scores()

    assert ServiceDetail.objects.get(team=team3).points == 85
