import json
from datetime import date
from decimal import Decimal
from zipfile import ZipFile

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from packets.models import Packet, PacketDistribution
from playwright.sync_api import expect
from registration.close_out_checks import scorecards_fingerprint
from registration.models import Event, EventResult, EventScoringEvidence, EventTeamAssignment, School, Season
from registration.services import current_snapshots
from registration.stage import ScorecardsEmailed
from scoring.models import (
    InjectScore,
    RedTeamScore,
    ServiceCorrection,
    ServiceCorrectionBatch,
    ServiceDetail,
    ServiceScore,
)
from scoring.service_corrections import portal_digest

from core.models import AuditLog, CompetitionConfig, QueuedTask
from team.models import Team

from .conftest import create_session_context

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def stopped_event():
    season = Season.objects.create(name="Synthetic Season", year=2030, is_active=True)
    event = Event.objects.create(
        season=season, name="Synthetic Invitational", event_type="invitational", date=date(2030, 1, 1), is_active=True
    )
    CompetitionConfig.objects.update_or_create(pk=1, defaults={"applications_enabled": False})
    packet = Packet.objects.create(
        event=event, title="Synthetic Packet", status="completed", uploaded_by="fixture",
        filename="example.pdf", file_data=b"%PDF-1.7", file_size=8, mime_type="application/pdf",
    )
    for slot, score in ((1, 100), (41, 200), (50, 150)):
        name = "Inactive Example" if slot != 50 else "Active Example"
        team = Team.objects.create(team_name=name, team_number=slot, is_active=slot == 50)
        EventTeamAssignment.objects.create(event=event, school=School.for_name(f"Example School {slot}"), team=team)
        PacketDistribution.objects.create(packet=packet, team=team, email_status="sent")
        InjectScore.objects.create(team=team, inject_id="1", inject_name="Memo", points_awarded=12, is_approved=True)
        ServiceScore.objects.create(team=team, service_points=score, sla_violations=-5)
        ServiceDetail.objects.create(team=team, service_name="Example-HTTPS", points=score, sla_violations=1)
    ServiceDetail.objects.create(
        team=Team.objects.get(team_number=41), service_name="Example-Private-Only", points=17,
    )
    finding = RedTeamScore.objects.create(is_approved=True, points_per_team=3)
    finding.affected_teams.add(Team.objects.get(team_number=41))
    batch = ServiceCorrectionBatch.objects.create(
        event=event, service_name="Example-HTTPS", ruling="example-full-void", manifest_sha256="a" * 64,
        baseline_sha256="b" * 64, portal_baseline_sha256=portal_digest("Example-HTTPS"), service_modifier="1.5",
    )
    for slot in (1, 50):
        ServiceCorrection.objects.create(
            batch=batch, team=Team.objects.get(team_number=slot), quotient_team_id=slot + 100,
            service_credit=-10, sla_credit=5,
        )
    return event, packet, batch


def _response(client, event, **fields):
    response = client.post(reverse("admin_competition_action"), {
        "action": "close_out_event", "open_event": event.pk, "confirm": "yolo", **fields,
    })
    return json.loads(b"".join(response.streaming_content).splitlines()[-1])


@pytest.mark.parametrize("failure", ["unassigned", "correction", "malformed", "incomplete", "wrong_school"])
def test_close_out_keeps_genuine_guards(stopped_event, gold_team_user, failure):
    event, packet, batch = stopped_event
    client = Client()
    client.force_login(gold_team_user)
    if failure == "unassigned":
        event.team_assignments.filter(team__team_number=41).delete()
        expected = "Team 41 has scores but no school"
    elif failure == "correction":
        batch.sync_conflict = True
        batch.save(update_fields=["sync_conflict"])
        expected = "Score sync conflicted with service correction batch"
    else:
        school, snapshot = next(iter(current_snapshots(event).items()))
        card = snapshot.scorecard
        if failure == "malformed":
            card["score"]["service_corrections"] = [{}]
        elif failure == "incomplete":
            del card["score"]["inject_points"]
        EventResult.objects.create(
            event=event, school_id=school, team_number=41 if failure == "wrong_school" else 50,
            rank=snapshot.standing.rank, total_score=snapshot.standing.total_score, scorecard=card,
        )
        expected = "does not match" if failure == "wrong_school" else "A saved scorecard cannot be read"
    saved_before = list(event.results.values())
    preview = client.post(reverse("admin_competition_action"), {"action": "close_out_check"}).json()
    assert expected in preview["problem"]
    if failure == "unassigned":
        assert "event roster owner" in preview["problem"] and preview["fixes"] == []
    queued = set(QueuedTask.objects.values_list("pk", flat=True))
    response = _response(client, event)
    assert not response["success"] and expected in response["message"]
    event.refresh_from_db()
    assert event.is_active and not event.is_finalized and list(event.results.values()) == saved_before
    assert not EventScoringEvidence.objects.filter(event=event).exists()
    assert ServiceScore.objects.count() == InjectScore.objects.count() == packet.distributions.count() == 3
    assert set(QueuedTask.objects.values_list("pk", flat=True)) == queued


@pytest.mark.browser
@pytest.mark.parametrize("source", ["current", "saved", "saved-only"])
def test_gold_freezes_only_existing_cards_and_archives_inactive_evidence(
    stopped_event, gold_team_user, live_server, pw_browser, mailoutbox, source,
):
    event, packet, batch = stopped_event
    saved_cards = source != "current"
    if saved_cards:
        Team.objects.filter(team_number=1).update(is_active=True)
    expected = current_snapshots(event)
    assert {s.standing.team.team_number for s in expected.values()} == ({1, 50} if saved_cards else {50})
    saved_ids = {}
    if saved_cards:
        Event.objects.filter(pk=event.pk).update(is_finalized=True)
        for school_id, snapshot in expected.items():
            standing = snapshot.standing
            saved_ids[school_id] = EventResult.objects.create(
                event=event, school_id=school_id, team_number=standing.team.team_number, rank=standing.rank,
                total_score=standing.total_score, scorecard=snapshot.scorecard,
            ).pk
        Team.objects.filter(team_number=1).update(is_active=False)
        if source == "saved":
            InjectScore.objects.filter(team__team_number=50).update(points_awarded=99)
        else:
            InjectScore.objects.all().delete()
            RedTeamScore.objects.all().delete()
            ServiceDetail.objects.all().delete()
            ServiceScore.objects.all().delete()
            ServiceCorrectionBatch.objects.filter(pk=batch.pk).update(revoked_at=timezone.now())
            Packet.objects.filter(pk=packet.pk).update(send_via_email=False)
            packet.distributions.update(email_status="pending")
    ScorecardsEmailed(event.pk, 1, 0, scorecards_fingerprint(event)).record("gold")
    if source == "saved-only":
        client = Client()
        client.force_login(gold_team_user)
        response = client.post(reverse("admin_competition_action"), {
            "action": "cancel_event", "open_event": event.pk, "confirm": "yolo",
        })
        assert response.status_code == 400 and "has scores, so it can't be cancelled" in response.json()["error"]
        assert Event.objects.get(pk=event.pk).is_active
    assignments = dict(event.team_assignments.values_list("team__team_number", "school_id"))
    with create_session_context(pw_browser, live_server, gold_team_user) as context:
        context.route(
            "**/*",
            lambda route: route.continue_() if route.request.url.startswith(live_server.url + "/") else route.abort(),
        )
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept("yolo"))
        page.goto(live_server.url + reverse("admin_competition"))
        slots = "41" if saved_cards else "1, 41"
        expect(page.get_by_role("link", name=f"Inactive slots {slots} have no card", exact=False)).to_be_visible()
        if saved_cards:
            saved_notice = f"will keep the {len(expected)} saved school scorecard(s)"
            expect(page.get_by_text(saved_notice, exact=False)).to_be_visible()
            expect(page.get_by_text("Saved scorecards differ from the last scorecard email record.")).to_be_visible()
        if source == "saved-only":
            expect(page.get_by_role("button", name="Cancel Event", exact=True)).to_have_count(0)
        close = page.get_by_role("button", name="Close Out Event")
        expect(close).to_be_enabled()
        close.click()
        expect(page.get_by_text(f"archived {len(expected)} school result(s)", exact=False)).to_be_visible()
        page.goto(live_server.url + reverse("registration_event_detail", args=[event.pk]))
        with page.expect_download() as download:
            page.get_by_role("link", name="Download Close Out Archive (ZIP)").click()
        with ZipFile(download.value.path()) as archive:
            assert not any("baseline" in name or "service_detail" in name for name in archive.namelist())
            assert all(b"Example-Private-Only" not in archive.read(name) for name in archive.namelist())
            scores = {row["team_number"]: row for row in json.loads(archive.read("final_scores.json"))["final_scores"]}
            raw_grades = json.loads(archive.read("inject_grades.json"))["inject_grades"]
            findings = json.loads(archive.read("red_findings.json"))["red_findings"]
            evidence = json.loads(archive.read("event_evidence.json"))
            assert set(evidence) == {"event_id", "captured_at", "assignments", "packets", "packet_distributions"}
    event.refresh_from_db()
    assert event.is_finalized and not event.is_active
    results = {result.school_id: result for result in event.results.all()}
    assert results.keys() == expected.keys()
    assert set(scores) == {result.team_number for result in results.values()}
    for school_id, result in results.items():
        before = expected[school_id]
        assert (result.rank, result.total_score, result.scorecard) == (
            before.standing.rank, before.standing.total_score, before.scorecard,
        )
        score = scores[result.team_number]
        assert (result.rank, result.total_score) == (score["rank"], Decimal(score["total_score"]))
        if saved_cards:
            assert result.pk == saved_ids[school_id]
    assert (scores[50]["raw_service_credit"], scores[50]["sla_credit"]) == (-10, 5)
    assert {a["team__team_number"]: a["school_id"] for a in evidence["assignments"]} == assignments
    assert {a["school_id"] for a in evidence["assignments"] if a["has_frozen_scorecard"]} == set(expected)
    private = EventScoringEvidence.objects.get(event=event).data
    raw = {row["team_id"]: Decimal(row["service_points"]) for row in private["service_scores"]}
    if source != "saved-only":
        assert {raw[a["team_id"]] for a in evidence["assignments"] if not a["team__is_active"]} == {100, 200}
        assert len(raw_grades) == 3
        assert any(
            row["service_name"] == "Example-Private-Only" and Decimal(row["points"]) == 17
            for row in private["service_details"]
        )
        assert [(row["affected_teams"], row["affected_team_numbers"]) for row in findings] == [
            (["Inactive Example"], [41])
        ]
    else:
        assert not raw and not private["service_details"]
    assert len(evidence["packet_distributions"]) == 3
    assert evidence["packets"][0]["send_via_email"] is (source != "saved-only")
    assert {row["email_status"] for row in evidence["packet_distributions"]} == {
        "pending" if source == "saved-only" else "sent"
    }
    assert list(Team.objects.filter(is_active=True).values_list("team_number", flat=True)) == [50]
    assert not AuditLog.objects.filter(action="team_activated").exists()
    assert not QueuedTask.objects.filter(task_type="sync_member_roles").exists()
    assert packet.distributions.count() == event.team_assignments.count() == 3
    assert ServiceCorrection.objects.filter(batch=batch, service_credit=-10, sla_credit=5).count() == 2
    assert not ServiceScore.objects.exists() and not mailoutbox
