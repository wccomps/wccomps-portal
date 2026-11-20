"""Service sync matches Quotient's export and uptime series to portal teams by the number in the team name."""

from decimal import Decimal

import httpx
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
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
# Paths that answer 500
FAILING: tuple[str, ...] = ()


@pytest.fixture(autouse=True)
def _quotient(settings, monkeypatch):
    settings.QUOTIENT_API_URL = "https://scoring.example"
    settings.QUOTIENT_USERNAME = "admin"
    settings.QUOTIENT_PASSWORD = "secret"
    cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in FAILING:
            return httpx.Response(500)
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


def _team_export(number: int, services: dict[str, int], sla_penalty: int = 0) -> dict[str, object]:
    return {
        "team_id": number,
        "team_name": f"team{number:02d}",
        "services": [
            {"service_name": name, "service_points": points, "sla_violations": 0, "sla_penalty": 0}
            for name, points in services.items()
        ],
        "gross_points": sum(services.values()),
        "total_sla_penalty": sla_penalty,
        "total_points": sum(services.values()) - sla_penalty,
    }


@pytest.mark.django_db
def test_sync_stores_the_points_and_the_sla_penalty_as_a_deduction(django_user_model) -> None:
    user = django_user_model.objects.create_user("gold")
    team3 = Team.objects.create(team_number=3, team_name="Three")

    result = sync_service_scores(user)

    assert result == {"teams_created": 1, "teams_updated": 0, "total": 1, "details_synced": 1}
    score = ServiceScore.objects.get(team=team3)
    assert (score.service_points, score.sla_violations, score.synced_by) == (Decimal("40"), Decimal("-5"), user)


@pytest.mark.django_db
def test_a_resync_counts_each_updated_team_and_every_service(monkeypatch) -> None:
    team5 = Team.objects.create(team_number=5, team_name="Five")
    Team.objects.create(team_number=3, team_name="Three")
    monkeypatch.setattr(
        "scoring.tests.test_quotient_service_sync.EXPORT",
        [_team_export(3, {"web": 40, "dns": 10}), _team_export(5, {"web": 30})],
    )

    first = sync_service_scores()
    second = sync_service_scores()

    assert first == {"teams_created": 2, "teams_updated": 0, "total": 2, "details_synced": 3}
    assert second == {"teams_created": 0, "teams_updated": 2, "total": 2, "details_synced": 3}
    # Quotient sent no uptime series for team 5
    assert ServiceDetail.objects.get(team=team5).uptime == 0


@pytest.mark.django_db
def test_teams_left_out_dont_stop_the_teams_after_them(monkeypatch) -> None:
    team3 = Team.objects.create(team_number=3, team_name="Three")
    Team.objects.create(team_number=4, team_name="Four")
    # No portal team 9; team 4 has no services
    monkeypatch.setattr(
        "scoring.tests.test_quotient_service_sync.EXPORT",
        [_team_export(9, {"web": 10}), _team_export(4, {}), *EXPORT],
    )

    result = sync_service_scores()

    assert result["total"] == 1
    assert ServiceDetail.objects.get().team == team3


@pytest.mark.django_db
def test_an_empty_export_syncs_nothing(monkeypatch) -> None:
    monkeypatch.setattr("scoring.tests.test_quotient_service_sync.EXPORT", [])

    assert sync_service_scores() == {"teams_created": 0, "teams_updated": 0, "total": 0, "details_synced": 0}


@pytest.mark.django_db
def test_a_score_that_cant_be_stored_names_its_team_and_each_problem(monkeypatch) -> None:
    Team.objects.create(team_number=3, team_name="Three")
    # A service name past 100 characters and points past decimal(10, 2), on a team total that fits
    team_export = {**_team_export(3, {"s" * 101: 10**10}), "gross_points": 10}
    monkeypatch.setattr("scoring.tests.test_quotient_service_sync.EXPORT", [team_export])

    with pytest.raises(ValueError, match=r"^Quotient's scores for team 3 can't be stored: Ensure [^;]+; Ensure [^;]+$"):
        sync_service_scores()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("failing", "message"),
    [
        ("/api/engine/export/scores", "Failed to retrieve service scores from Quotient"),
        ("/api/graphs/uptimes", "Failed to retrieve uptimes from Quotient"),
    ],
)
def test_the_scoring_page_says_which_quotient_read_failed(monkeypatch, gold_team_user, failing, message) -> None:
    Team.objects.create(team_number=3, team_name="Three")
    monkeypatch.setattr("scoring.tests.test_quotient_service_sync.FAILING", (failing,))
    client = Client()
    client.force_login(gold_team_user)

    response = client.post(reverse("scoring:sync_scores"), follow=True)

    assert f"Failed to sync scores: {message}" in response.content.decode()
