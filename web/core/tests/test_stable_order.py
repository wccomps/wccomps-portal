"""Rows that tie on a list's sort column keep one order (the model's default ordering, then pk), so paging through
a list shows each row once."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from scoring.models import IncidentReport, InjectScore

from team.models import Team

pytestmark = pytest.mark.django_db


def test_inject_grades_that_tie_on_the_inject_come_in_team_order(admin_client: Client, admin_user: User) -> None:
    for number in (5, 2, 9, 1):
        team = Team.objects.create(team_number=number, team_name=f"Team {number}")
        InjectScore.objects.create(
            team=team,
            inject_id="1",
            inject_name="Inject",
            max_points=Decimal(10),
            points_awarded=Decimal(5),
            graded_by=admin_user,
        )
    for sort in ("inject_name", "-inject_name", "points_awarded"):
        rows = admin_client.get(reverse("scoring:inject_grades_review"), {"sort": sort}).context["page_obj"]
        assert [row.team.team_number for row in rows] == [1, 2, 5, 9], sort


def test_incidents_that_tie_on_the_sort_come_in_id_order(admin_client: Client, admin_user: User) -> None:
    team = Team.objects.create(team_number=1, team_name="Team 1")
    when = timezone.now() - timedelta(hours=1)
    for pk in (30, 10, 20):
        IncidentReport.objects.create(
            pk=pk,
            team=team,
            submitted_by=admin_user,
            attack_description="x",
            source_ip="10.0.0.1",
            attack_detected_at=when,
        )
    IncidentReport.objects.update(created_at=when)
    for sort in ("created_at", "-created_at", "attack_detected_at", "team__team_number"):
        rows = admin_client.get(reverse("scoring:review_incidents"), {"sort": sort}).context["page_obj"]
        assert [row.pk for row in rows] == [10, 20, 30], sort
