import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from packets.models import Packet, PacketDistribution
from playwright.sync_api import expect
from registration.close_out_checks import scorecards_fingerprint
from registration.models import (
    Event,
    EventArchive,
    EventResult,
    EventScoringEvidence,
    EventTeamAssignment,
    School,
    Season,
)
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
        InjectScore.objects.create(
            team=team, inject_id="1", inject_name="Memo", points_awarded=12, is_approved=True,
            feedback=f"Feedback for slot {slot}", feedback_approved=slot == 41,
        )
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
    fields.setdefault("preview", "0" * 64)
    response = client.post(reverse("admin_competition_action"), {
        "action": "close_out_event", "open_event": event.pk, "confirm": "yolo", **fields,
    })
    return json.loads(b"".join(response.streaming_content).splitlines()[-1])


@pytest.mark.parametrize(
    "failure",
    ["unassigned", "correction", "malformed", "incomplete", "details", "cohort",
     "wrong_school", "missing_category", "detail_rank", "category_bound", "neighbor_bound", "missing_detail_rows",
     "self_neighbor", "duplicate_neighbor", "missing_neighbor", "neighbor_gap", "neighbor_order", "detail_sum",
     "modifier", "weight", "version"],
)
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
        elif failure == "details":
            card["stats"] = {}
        elif failure == "cohort":
            card["stats"]["team_count"] = 0
        elif failure == "missing_category":
            del card["stats"]["category_ranks"]["services"]
        elif failure == "detail_rank":
            card["stats"]["service_stats"][0]["rank"] = "1"
        elif failure == "category_bound":
            card["stats"]["category_ranks"]["services"]["rank"] = card["stats"]["team_count"] + 1
        elif failure == "neighbor_bound":
            card["stats"]["neighbors"] = [{"rank": card["stats"]["team_count"] + 1, "total_score": 7, "gap": 5}]
        elif failure == "missing_detail_rows":
            card["stats"]["inject_stats"] = []
            card["inject_total"] = 1
        elif failure == "detail_sum":
            card["inject_total"] = {"$d": "10"}
            card["stats"]["inject_stats"][0]["points"] = 0
        elif failure.startswith("neighbor_") or failure.endswith("_neighbor"):
            total = snapshot.score.total
            def neighbor(rank, score, gap):
                return {"rank": rank, "total_score": {"$d": str(score)}, "gap": {"$d": str(gap)}}

            stats = card["stats"]
            stats["team_count"] = 1 if failure == "self_neighbor" else 2
            stats["neighbors"] = {
                "self_neighbor": [neighbor(1, total, 0)],
                "duplicate_neighbor": [neighbor(2, total, 0)] * 2,
                "missing_neighbor": [],
                "neighbor_gap": [neighbor(2, total, -1)],
                "neighbor_order": [neighbor(2, total + 1, 1)],
            }[failure]
        elif failure == "modifier":
            card["scaling"]["service_modifier"] = -1
        elif failure == "weight":
            card["scaling"]["service_weight"] = 99
        EventResult.objects.create(
            event=event, school_id=school, team_number=41 if failure == "wrong_school" else 50,
            rank=snapshot.score.rank, total_score=snapshot.score.total, scorecard=card,
            version=2 if failure == "version" else 1,
        )
        expected = {
            "wrong_school": "do not match",
            "version": "uses an unsupported version",
        }.get(failure, "A saved scorecard cannot be read")
        retained = EventArchive.objects.create(event=event, data=b"retained", retained_kind="finalize")
    assignments_before = list(event.team_assignments.order_by("pk").values())
    score_models = (ServiceScore, ServiceDetail, InjectScore)
    scores_before = [list(model.objects.order_by("pk").values()) for model in score_models]
    saved_before = list(event.results.values())
    preview = client.post(reverse("admin_competition_action"), {"action": "close_out_check"}).json()
    assert expected in preview["problem"]
    assert not preview["preview"]
    if failure == "unassigned":
        assert "event roster owner" in preview["problem"] and preview["fixes"] == []
    if failure == "wrong_school":
        assert "Example School 50 is currently assigned Team 50" in preview["problem"]
        assert "has a saved card for Team 41" in preview["problem"]
    queued = set(QueuedTask.objects.values_list("pk", flat=True))
    response = _response(client, event)
    assert not response["success"] and expected in response["message"]
    event.refresh_from_db()
    assert event.is_active and not event.is_finalized and list(event.results.values()) == saved_before
    if failure not in ("unassigned", "correction"):
        kept = EventArchive.objects.get(event=event)
        assert (bytes(kept.data), kept.updated_at, kept.close_out_data) == (
            b"retained", retained.updated_at, None,
        )
    assert not EventScoringEvidence.objects.filter(event=event).exists()
    assert list(event.team_assignments.order_by("pk").values()) == assignments_before
    assert [list(model.objects.order_by("pk").values()) for model in score_models] == scores_before
    assert ServiceScore.objects.count() == InjectScore.objects.count() == packet.distributions.count() == 3
    assert set(QueuedTask.objects.values_list("pk", flat=True)) == queued


@pytest.mark.parametrize("conflict", ["inverted-ranks", "mixed-cohorts", "neighbor-total"])
def test_saved_cards_agree_on_cohort_rank_and_neighbor_total(stopped_event, gold_team_user, conflict):
    event, _, _ = stopped_event
    Team.objects.filter(team_number=1).update(is_active=True)
    client = Client()
    client.force_login(gold_team_user)
    url = reverse("admin_competition_action")
    preview = client.post(url, {"action": "close_out_check"}).json()["preview"]
    for school, snapshot in current_snapshots(event).items():
        rank = 3 - snapshot.score.rank if conflict == "inverted-ranks" else snapshot.score.rank
        stats = snapshot.scorecard["stats"]
        if conflict == "mixed-cohorts" and rank == 1:
            stats["team_count"] = 3
        if conflict == "inverted-ranks":
            peer_rank = 3 - rank
            stats["neighbors"] = [{
                "rank": peer_rank, "total_score": {"$d": str(snapshot.score.total)}, "gap": 0,
            }]
        if conflict == "neighbor-total" and rank == 1:
            claimed = snapshot.score.total - Decimal("100.00")
            stats["neighbors"] = [{
                "rank": 2, "total_score": {"$d": str(claimed)}, "gap": -100,
            }]
        snapshot.scorecard["score"]["rank"] = rank
        EventResult.objects.create(
            event=event, school_id=school, team_number=snapshot.score.team_number,
            rank=rank, total_score=snapshot.score.total, scorecard=snapshot.scorecard,
        )
    saved_before = list(event.results.values())
    archive = EventArchive.objects.create(event=event, data=b"old ZIP", retained_kind="finalize")
    expected = {
        "mixed-cohorts": "Saved result cohorts conflict",
        "inverted-ranks": "Saved result ranks conflict",
        "neighbor-total": "Saved scorecards disagree on the total for Rank 2",
    }[conflict]
    refused = client.post(url, {"action": "close_out_check"}).json()
    assert expected in refused["problem"] and not refused["preview"]
    assert expected in _response(client, event, preview=preview)["message"]
    assert ServiceScore.objects.count() == 3 and Event.objects.get(pk=event.pk).is_active
    assert list(event.results.values()) == saved_before
    archive.refresh_from_db()
    assert bytes(archive.data) == b"old ZIP" and archive.close_out_data is None
    assert not EventScoringEvidence.objects.filter(event=event).exists()


@pytest.mark.browser
def test_stale_gold_preview_refuses_before_archiving_or_wiping(stopped_event, gold_team_user, live_server, pw_browser):
    event, _, _ = stopped_event
    Team.objects.filter(team_number=1).update(is_active=True)
    with create_session_context(pw_browser, live_server, gold_team_user) as context:
        context.route(
            "**/*",
            lambda route: route.continue_() if route.request.url.startswith(live_server.url + "/") else route.abort(),
        )
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept("yolo"))
        page.goto(live_server.url + reverse("admin_competition"))
        expect(page.get_by_text("Team 1 (Example School 1):", exact=False)).to_be_visible()
        Team.objects.filter(team_number=1).update(is_active=False)
        page.get_by_role("button", name="Close Out Event").click()
        expect(page.get_by_text("The Close Out preview changed.", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Close Out Event")).to_be_disabled()
        assert Event.objects.get(pk=event.pk).is_active and ServiceScore.objects.count() == 3
        assert not event.results.exists() and not EventArchive.objects.filter(event=event).exists()
        page.reload()
        expect(page.get_by_text("Team 1 (Example School 1, inactive)", exact=False)).to_be_visible()
        page.get_by_role("button", name="Close Out Event").click()
        expect(page.get_by_text("archived 1 school result(s)", exact=False)).to_be_visible()


@pytest.mark.browser
def test_preview_response_for_another_open_event_requires_reload(
    stopped_event, gold_team_user, live_server, pw_browser,
):
    event, _, _ = stopped_event
    other = Event.objects.create(
        season=event.season, name="Another event", date=event.date, event_type=event.event_type,
    )
    with create_session_context(pw_browser, live_server, gold_team_user) as context:
        def route_request(route):
            request = route.request
            if not request.url.startswith(live_server.url + "/"):
                route.abort()
                return
            if request.method == "POST" and request.url.endswith(reverse("admin_competition_action")):
                Event.objects.filter(pk=event.pk).update(is_active=False)
                Event.objects.filter(pk=other.pk).update(is_active=True)
            route.continue_()

        context.route("**/*", route_request)
        page = context.new_page()
        page.goto(live_server.url + reverse("admin_competition"))
        expect(page.get_by_text("Couldn't preview Close Out.", exact=False)).to_be_visible()
        expect(page.get_by_role("button", name="Close Out Event")).to_be_disabled()
    assert not EventArchive.objects.exists()
    assert ServiceScore.objects.count() == 3


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
    assert {s.score.team_number for s in expected.values()} == ({1, 50} if saved_cards else {50})
    saved_ids = {}
    if saved_cards:
        Event.objects.filter(pk=event.pk).update(is_finalized=True)
        for school_id, snapshot in expected.items():
            score = snapshot.score
            saved_ids[school_id] = EventResult.objects.create(
                event=event, school_id=school_id, team_number=score.team_number, rank=score.rank,
                total_score=score.total, scorecard=snapshot.scorecard,
            ).pk
        Team.objects.filter(team_number=1).update(is_active=False)
        if source == "saved":
            InjectScore.objects.filter(team__team_number=50).update(points_awarded=99)
            team = Team.objects.create(team_number=37, team_name="Extra active", is_active=True)
            EventTeamAssignment.objects.create(event=event, team=team, school=School.for_name("Omitted school"))
        else:
            InjectScore.objects.all().delete()
            RedTeamScore.objects.all().delete()
            ServiceDetail.objects.all().delete()
            ServiceScore.objects.all().delete()
            ServiceCorrectionBatch.objects.filter(pk=batch.pk).update(revoked_at=timezone.now())
            Packet.objects.filter(pk=packet.pk).update(send_via_email=False)
            packet.distributions.update(email_status="pending")
    if source != "saved-only":
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
        expect(page.get_by_text("Team 41 (Example School 41, inactive)", exact=False)).to_be_visible()
        if saved_cards:
            expect(page.get_by_text("Saved school scorecards to keep", exact=True)).to_be_visible()
        if source == "saved":
            expect(page.get_by_text("Team 37 (Omitted school, active)", exact=False)).to_be_visible()
            expect(page.get_by_text("Saved scorecards differ from the last scorecard email record.")).to_be_visible()
        if source == "saved-only":
            expect(page.get_by_text("No scorecard email is recorded for this event.")).to_be_visible()
            event_panel = page.locator("#event-panel")
            expect(event_panel.get_by_text("Close out the event with Close Out Event", exact=False)).to_be_visible()
            expect(page.locator("#steps_list").get_by_role("link", name="Email All Scorecards")).to_have_count(0)
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
            before.score.rank, before.score.total, before.scorecard,
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
        assert {row["team_number"]: row["approved_feedback"] for row in raw_grades} == {
            1: "", 41: "Feedback for slot 41", 50: "",
        }
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
    active = set(Team.objects.filter(is_active=True).values_list("team_number", flat=True))
    assert active == ({37, 50} if source == "saved" else {50})
    assert not AuditLog.objects.filter(action="team_activated").exists()
    assert not QueuedTask.objects.filter(task_type="sync_member_roles").exists()
    assert packet.distributions.count() == 3 and event.team_assignments.count() == len(assignments)
    assert ServiceCorrection.objects.filter(batch=batch, service_credit=-10, sla_credit=5).count() == 2
    assert not ServiceScore.objects.exists() and not mailoutbox


@pytest.mark.browser
def test_historical_v1_card_and_finalize_zip_survive_close_out(
    stopped_event, gold_team_user, live_server, pw_browser,
):
    event, _, _ = stopped_event
    team = Team.objects.create(team_number=37, team_name="Legacy slot")
    school = School.for_name("Legacy school")
    EventTeamAssignment.objects.create(event=event, team=team, school=school)
    fixture = Path(__file__).parents[1] / "scoring/tests/fixtures/scorecard_snapshot_v1.json"
    card = json.loads(fixture.read_text())
    # The template fixture uses distinct sentinel values, not arithmetic produced by the writer.
    card["red_total"] = {"$d": "5501.00"}
    card["inject_total"] = {"$d": "5201.00"}
    card["orange_total"] = {"$d": "5601.00"}
    card["service_total"] = {"$d": "5101.00"}
    card["service_sla_total"] = 5103
    card["stats"]["neighbors"] = [
        {"rank": 1, "total_score": {"$d": "3808.00"}, "gap": 0},
        {"rank": 3, "total_score": {"$d": "3807.99"}, "gap": {"$d": "-0.01"}},
    ]
    result = EventResult.objects.create(
        event=event, school=school, team_number=37, rank=2, total_score="3808.00", scorecard=card,
    )
    old_zip = BytesIO()
    with ZipFile(old_zip, "w") as archive:
        archive.writestr("original-finalize.txt", "Original synthetic ZIP")
    historical = EventArchive.objects.create(event=event, data=old_zip.getvalue(), retained_kind="finalize")
    prompts = []
    with create_session_context(pw_browser, live_server, gold_team_user) as context:
        context.route(
            "**/*",
            lambda route: route.continue_() if route.request.url.startswith(live_server.url + "/") else route.abort(),
        )
        page = context.new_page()
        archive_url = reverse("registration_event_archive", args=[event.pk])
        assert page.request.get(live_server.url + archive_url).status == 404
        assert page.request.get(live_server.url + archive_url + "?version=finalize").body() == old_zip.getvalue()

        def confirm(dialog):
            prompts.append(dialog.message)
            dialog.accept("yolo")

        page.on("dialog", confirm)
        page.goto(live_server.url + reverse("admin_competition"))
        expect(page.get_by_text("Team 37 (Legacy school): Rank 2, 3808.00 points")).to_be_visible()
        expect(page.get_by_text("Preserve required files", exact=False)).to_be_visible()
        page.get_by_role("button", name="Close Out Event").click()
        expect(page.get_by_text("archived 1 school result(s)", exact=False)).to_be_visible()
        assert "If Nextcloud is configured when cleanup runs, it can delete team accounts" in prompts[0]
        page.goto(live_server.url + reverse("registration_event_detail", args=[event.pk]))
        with page.expect_download() as old:
            page.get_by_role("link", name="Download retained Finalize Archive (ZIP)").click()
        assert old.value.path().read_bytes() == old_zip.getvalue()
        with page.expect_download() as new:
            page.get_by_role("link", name="Download Close Out Archive (ZIP)").click()
        with ZipFile(new.value.path()) as archive:
            row, = json.loads(archive.read("final_scores.json"))["final_scores"]
            assert (row["rank"], row["total_score"], row["service_points"]) == (2, "3808.00", "3101.00")
            assert "exported_at" in row and "calculated_at" not in row
    assert EventArchive.objects.get(event=event).updated_at == historical.updated_at
    result.refresh_from_db()
    assert result.scorecard == card
    unprivileged = Client()
    unprivileged.force_login(User.objects.create_user("unprivileged"))
    archive_url = reverse("registration_event_archive", args=[event.pk])
    assert unprivileged.get(archive_url).status_code == 403
    assert unprivileged.get(archive_url + "?version=finalize").status_code == 403


@pytest.mark.browser
def test_archive_only_event_gets_reconciliation_not_cancel_or_close_out(
    stopped_event, gold_team_user, live_server, pw_browser,
):
    event, _, _ = stopped_event
    InjectScore.objects.all().delete()
    RedTeamScore.objects.all().delete()
    ServiceDetail.objects.all().delete()
    ServiceScore.objects.all().delete()
    EventArchive.objects.create(event=event, data=b"historical bytes")
    message = "This open event has an archive but no saved scorecards."
    with create_session_context(pw_browser, live_server, gold_team_user) as context:
        context.route(
            "**/*",
            lambda route: route.continue_() if route.request.url.startswith(live_server.url + "/") else route.abort(),
        )
        page = context.new_page()
        page.goto(live_server.url + reverse("admin_competition"))
        expect(page.locator("#close-out").get_by_text(message, exact=False)).to_be_visible()
        expect(page.get_by_text("Needs archive reconciliation", exact=True)).to_be_visible()
        expect(page.get_by_text(message, exact=False)).to_have_count(2)
        expect(page.get_by_role("button", name="Start Competition Again")).to_have_count(0)
        expect(page.get_by_role("button", name="Cancel Event", exact=True)).to_have_count(0)
        expect(page.get_by_role("button", name="Close Out Event", exact=True)).to_have_count(0)
    client = Client()
    client.force_login(gold_team_user)
    url = reverse("admin_competition_action")
    canceled = client.post(url, {"action": "cancel_event", "open_event": event.pk, "confirm": "yolo"})
    assert canceled.status_code == 400 and message in canceled.json()["error"]
    assert message in _response(client, event)["message"]
    assert message in client.post(reverse("school_info_clear")).content.decode()
    assert Event.objects.get(pk=event.pk).is_active and event.team_assignments.count() == 3
    retained = EventArchive.objects.get(event=event)
    assert retained.data is not None and bytes(retained.data) == b"historical bytes"
    assert not event.results.exists() and not QueuedTask.objects.filter(task_type="reconcile_teams").exists()
    archive_url = reverse("registration_event_archive", args=[event.pk])
    assert client.get(archive_url).status_code == 404
    assert client.get(archive_url + "?version=historical").content == b"historical bytes"
