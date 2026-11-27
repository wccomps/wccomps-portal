"""Retained ZIP type follows the old writer's audit, not later result rows."""

from datetime import date, timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.urls import reverse

from registration.models import EventArchive

pytestmark = pytest.mark.django_db(transaction=True)


def test_legacy_downloads_keep_bytes_and_timestamp_under_the_proven_capture_type(gold_team_user):
    before = ("registration", "0016_remove_teamregistration_edit_token")
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    executor.migrate([before])
    targets = [before, ("core", "0027_queuedtask_sync_quotient_metadata")]
    old = MigrationExecutor(connection).loader.project_state(targets).apps
    try:
        season = old.get_model("registration", "Season").objects.create(name="Synthetic", year=2030)
        event_model = old.get_model("registration", "Event")
        archive_model = old.get_model("registration", "EventArchive")
        audit_model = old.get_model("core", "AuditLog")
        captures = {}
        scenarios = {
            "close_out": (("event_closed_out", 1),),
            "finalize": (("event_finalized", 1),),
            "historical": (("event_closed_out", -1),),
            "conflict": (("event_finalized", 1), ("event_closed_out", 2)),
        }
        for label, audits in scenarios.items():
            event = event_model.objects.create(
                season=season, name=label, date=date(2030, 1, 1), event_type="invitational",
            )
            row = archive_model.objects.create(event=event, data=label.encode())
            captures[label] = (event.pk, row.updated_at)
            for action, offset in audits:
                entry = audit_model.objects.create(
                    action=action, target_entity="event", target_id=event.pk, admin_user="gold",
                )
                audit_model.objects.filter(pk=entry.pk).update(created_at=row.updated_at + timedelta(seconds=offset))
    finally:
        MigrationExecutor(connection).migrate(latest)

    client = Client()
    client.force_login(gold_team_user)
    for label, (event_id, timestamp) in captures.items():
        version = "historical" if label == "conflict" else label
        archive = EventArchive.objects.get(event_id=event_id)
        assert archive.updated_at == timestamp
        url = reverse("registration_event_archive", args=[event_id])
        for requested in ("close_out", "historical", "finalize"):
            response = client.get(url if requested == "close_out" else f"{url}?version={requested}")
            if requested == version:
                assert response.status_code == 200 and response.content == label.encode()
            else:
                assert response.status_code == 404
