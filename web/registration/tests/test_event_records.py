"""The records Finalize writes: a school's frozen result per event, and the event's export zip."""

import zipfile
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from django.db import IntegrityError
from scoring.export import export_all_zip_bytes

from registration.models import Event, EventResult, School, Season

pytestmark = pytest.mark.django_db


@pytest.fixture
def event():
    season = Season.objects.create(name="2026", year=2026, is_active=True)
    return Event.objects.create(season=season, name="Inv", event_type="invitational", date=date(2026, 10, 3))


def _result(event, school, rank, team_number):
    return EventResult.objects.create(
        event=event, school=school, team_number=team_number, rank=rank, total_score=Decimal("1.00"), scorecard={}
    )


def test_results_order_by_rank_with_unranked_last(event):
    _result(event, School.for_name("C"), None, 3)
    _result(event, School.for_name("B"), 2, 2)
    _result(event, School.for_name("A"), 1, 1)

    assert [r.school.name for r in EventResult.objects.filter(event=event)] == ["A", "B", "C"]


def test_one_result_per_school_per_event(event):
    school = School.for_name("A")
    _result(event, school, 1, 1)

    with pytest.raises(IntegrityError):
        _result(event, school, 2, 2)


def test_export_zip_bytes_holds_every_dataset():
    names = zipfile.ZipFile(BytesIO(export_all_zip_bytes())).namelist()

    assert "final_scores.csv" in names and "tickets.json" in names
