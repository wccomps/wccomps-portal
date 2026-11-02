"""The incident and red finding pages render their pickers and outcomes as the form's own fields: a fieldset whose
legend is the form's label, its help and errors, and what the page's script reads (data-points, the hidden select)."""

import re
from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse

from scoring.models import QuotientMetadataCache, RedTeamScore
from team.models import Team

pytestmark = pytest.mark.django_db

BOXES = [{"name": "web", "ip": "10.0.0.20", "services": []}, {"name": "db", "ip": "10.0.0.30", "services": []}]
PAGES = [("scoring:submit_incident_report", "blue_team_user"), ("scoring:submit_red_score", "red_team_user")]


@pytest.fixture(autouse=True)
def boxes(mock_quotient_client: object) -> None:
    QuotientMetadataCache.objects.create(boxes=BOXES)


@pytest.fixture
def page(request: pytest.FixtureRequest) -> Callable[..., str]:
    def render(url_name: str, user_fixture: str, data: dict[str, object] | None = None) -> str:
        client = Client()
        client.force_login(request.getfixturevalue(user_fixture))
        url = reverse(url_name)
        response = client.post(url, data) if data is not None else client.get(url)
        assert response.status_code == 200
        return response.content.decode()

    return render


def group(html: str, legend: str) -> str:
    """The field group for the field labelled `legend`: its fieldset, then its help and errors."""
    found = re.search(
        rf"<fieldset[^>]*>\s*<legend[^>]*>{re.escape(legend)}:</legend>.*?</fieldset>(?:\s*<p[^>]*>.*?</p>)*",
        html,
        re.S,
    )
    assert found, f"no field group with legend {legend!r}"
    return found.group(0)


def tag(html: str, pattern: str) -> str:
    found = re.search(rf"<[^>]*{pattern}[^>]*>", html)
    assert found, pattern
    return found.group(0)


@pytest.mark.parametrize(("url_name", "user_fixture"), PAGES)
def test_affected_boxes_is_a_field_group_of_toggle_buttons_over_a_hidden_select(
    page: Callable[..., str], url_name: str, user_fixture: str
) -> None:
    boxes = group(page(url_name, user_fixture), "Affected Boxes")

    select = tag(boxes, r'name="affected_boxes"')
    assert select.startswith("<select") and " multiple" in select and " hidden" in select
    assert 'id="id_affected_boxes"' in select and " required" not in select
    for name in ("web", "db"):
        button = tag(boxes, rf'data-value="{name}"')
        assert 'aria-pressed="false"' in button


@pytest.mark.parametrize(("url_name", "user_fixture"), PAGES)
def test_a_failed_submit_shows_the_picked_boxes_pressed(
    page: Callable[..., str], url_name: str, user_fixture: str
) -> None:
    boxes = group(page(url_name, user_fixture, {"affected_boxes": ["db"]}), "Affected Boxes")

    assert 'aria-pressed="true"' in tag(boxes, r'data-value="db"')
    assert 'aria-pressed="false"' in tag(boxes, r'data-value="web"')
    assert re.search(r'<option value="db"\s+selected>', boxes)


def test_affected_teams_is_a_field_group_with_its_help_errors_and_select_all(page: Callable[..., str]) -> None:
    teams = [Team.objects.create(team_number=n, team_name=f"Team {n:02d}", is_active=True) for n in (3, 4)]
    teams_group = group(page("scoring:submit_red_score", "red_team_user", {}), "Affected Teams")

    fieldset = tag(teams_group, "fieldset")
    assert 'aria-describedby="id_affected_teams_helptext id_affected_teams_error"' in fieldset
    assert 'id="id_affected_teams_helptext"' in teams_group and 'id="id_affected_teams_error"' in teams_group
    assert " hidden" in tag(teams_group, r'name="affected_teams"')
    for team in teams:
        assert re.search(
            rf'<button[^>]*data-value="{team.pk}"[^>]*>\s*Team {team.team_number}\s*</button>', teams_group
        )
    assert tag(teams_group, r'data-pick="all"') and tag(teams_group, r'data-pick="none"')


def test_source_ip_type_is_a_field_group_labelled_source_ip_that_shows_its_error(page: Callable[..., str]) -> None:
    source = group(page("scoring:submit_red_score", "red_team_user", {"source_ip_type": "bogus"}), "Source IP")

    assert tag(source, r'name="source_ip_type"').startswith("<input")
    assert 'id="id_source_ip_type_error"' in source and "Select a valid choice" in source


def test_each_outcome_takes_its_label_help_and_points_from_the_outcome_table(page: Callable[..., str]) -> None:
    html = page("scoring:submit_red_score", "red_team_user")

    for rank, outcome in enumerate(RedTeamScore.OUTCOMES):
        checkbox = tag(html, rf'name="{outcome.field}"')
        assert f'data-points="{outcome.points}"' in checkbox
        assert re.search(
            rf'<label for="id_{outcome.field}"[^>]*>{re.escape(f"{outcome.label} (-{outcome.points})")}</label>', html
        ), rank
        assert f'<p class="help-text" id="id_{outcome.field}_helptext">{outcome.description}</p>' in html


def test_a_finding_on_many_boxes_keeps_every_box_s_ip_template():
    from django.db import models

    from scoring.models import RedTeamScore

    assert isinstance(RedTeamScore._meta.get_field("destination_ip_template"), models.TextField)
