import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from core.models import UserGroups
from orange_team.models import (
    OrangeAssignment,
    OrangeAssignmentResult,
    OrangeCheck,
    OrangeCheckCriterion,
)
from team.models import Team

pytestmark = pytest.mark.django_db


class TestAssignmentSave:
    def _setup(
        self,
    ) -> tuple[
        User,
        OrangeAssignment,
        OrangeCheckCriterion,
        OrangeCheckCriterion,
        OrangeAssignmentResult,
        OrangeAssignmentResult,
    ]:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        c1 = OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        c2 = OrangeCheckCriterion.objects.create(orange_check=check, label="C2", points=3)
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        r1 = OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c1)
        r2 = OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c2)
        return user, assignment, c1, c2, r1, r2

    def test_save_criterion(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert response.status_code == 200
        data = response.json()
        assert data["score"] == 5
        assert data["max_score"] == 8
        r1.refresh_from_db()
        assignment.refresh_from_db()
        assert r1.met is True
        assert assignment.score == 5

    def test_saved_score_persists_on_dashboard_reload_and_after_setting_reminder(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        client = Client()
        client.force_login(user)
        client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )

        dashboard_html = client.get("/orange-team/").content.decode()
        assert 'data-score="5"' in dashboard_html
        assert "5 / 8" in dashboard_html

        reminder_response = client.post(
            "/orange-team/followups/create/",
            {"assignment_id": assignment.pk, "minutes": "15", "note": "Recheck"},
            follow=True,
        )
        reminder_html = reminder_response.content.decode()
        assert 'data-score="5"' in reminder_html
        assert "5 / 8" in reminder_html

    def test_save_updates_status_to_in_progress(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        assert assignment.status == "pending"
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assignment.refresh_from_db()
        assert assignment.status == "in_progress"

    def test_cannot_save_submitted_assignment(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        assignment.status = "submitted"
        assignment.save()
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_the_old_volunteers_autosave_after_a_reassignment_is_refused(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        new_volunteer = User.objects.create_user(username="orange2", password="test")
        OrangeAssignment.objects.filter(pk=assignment.pk).update(user=new_volunteer)
        client = Client()
        client.force_login(user)
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert response.status_code == 404
        assignment.refresh_from_db()
        r1.refresh_from_db()
        assert (assignment.user, assignment.status, r1.met) == (new_volunteer, "pending", False)

    def test_other_user_cannot_save(self) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        other = User.objects.create_user(username="orange2", password="test")
        UserGroups.objects.create(user=other, authentik_id="o2", groups=["WCComps_OrangeTeam"])
        client = Client()
        client.force_login(User.objects.get(username="orange2"))
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert response.status_code == 404

    def test_save_range_and_zero_point_criteria_and_clamp_on_edit(self, gold_team_user: User) -> None:
        user, assignment, c1, c2, r1, r2 = self._setup()
        c1.points = 0
        c1.save()
        c2.points = 10
        c2.criterion_type = OrangeCheckCriterion.CriterionType.RANGE
        c2.save()

        client = Client()
        client.force_login(user)

        resp_zero = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c1.pk, "met": True}),
            content_type="application/json",
        )
        assert resp_zero.status_code == 200
        assert resp_zero.json() == {"score": 0, "max_score": 10}
        r1.refresh_from_db()
        assert r1.met is True

        resp_range = client.post(
            f"/orange-team/assignments/{assignment.pk}/save/",
            data=json.dumps({"criterion_id": c2.pk, "points": 7}),
            content_type="application/json",
        )
        assert resp_range.status_code == 200
        assert resp_range.json() == {"score": 7, "max_score": 10}
        r2.refresh_from_db()
        assignment.refresh_from_db()
        assert r2.points_awarded == 7
        assert assignment.score == 7

        for invalid in (
            {"criterion_id": c2.pk, "points": 11},
            {"criterion_id": c2.pk, "points": -1},
            {"criterion_id": c2.pk, "met": True},
            {"criterion_id": c1.pk, "points": 0},
        ):
            bad_resp = client.post(
                f"/orange-team/assignments/{assignment.pk}/save/",
                data=json.dumps(invalid),
                content_type="application/json",
            )
            assert bad_resp.status_code == 400

        lead_client = Client()
        lead_client.force_login(gold_team_user)
        edit_resp = lead_client.post(
            f"/orange-team/checks/{assignment.orange_check_id}/edit/",
            {
                "title": "Test",
                "criterion_id_0": str(c1.pk),
                "criterion_label_0": "C1",
                "criterion_points_0": "0",
                "criterion_type_0": "checkbox",
                "criterion_id_1": str(c2.pk),
                "criterion_label_1": "C2",
                "criterion_points_1": "5",
                "criterion_type_1": "range",
            },
        )
        assert edit_resp.status_code == 302
        r2.refresh_from_db()
        assignment.refresh_from_db()
        assert r2.points_awarded == 5
        assert assignment.score == 5


class TestAssignmentSubmit:
    def test_submit_assignment(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        c1 = OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c1, met=True)
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(f"/orange-team/assignments/{assignment.pk}/submit/")
        assert response.status_code == 302
        assignment.refresh_from_db()
        assert assignment.status == "submitted"
        assert assignment.score == 5
        assert assignment.submitted_at is not None

    def test_submit_assignment_with_evaluator_notes_and_screenshots(self) -> None:
        from django.core.files.uploadedfile import SimpleUploadedFile

        from orange_team.models import OrangeAssignmentScreenshot

        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        other = User.objects.create_user(username="orange2", password="test")
        UserGroups.objects.create(user=other, authentik_id="o2", groups=["WCComps_OrangeTeam"])
        lead = User.objects.create_user(username="lead1", password="test")
        UserGroups.objects.create(user=lead, authentik_id="l1", groups=["WCComps_OrangeTeam_Lead"])

        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Phone Check", description="Desc")
        c1 = OrangeCheckCriterion.objects.create(orange_check=check, label="C1", points=5)
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team)
        OrangeAssignmentResult.objects.create(assignment=assignment, criterion=c1, met=False)

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        upload = SimpleUploadedFile("call-log.png", png_bytes, content_type="image/png")

        client = Client()
        client.force_login(user)
        response = client.post(
            f"/orange-team/assignments/{assignment.pk}/submit/",
            {
                "evaluator_notes": "Team did not answer after 3 attempts.",
                "screenshots": [upload],
            },
        )
        assert response.status_code == 302
        assignment.refresh_from_db()
        assert assignment.status == "submitted"
        assert assignment.evaluator_notes == "Team did not answer after 3 attempts."
        screenshot = OrangeAssignmentScreenshot.objects.get(assignment=assignment)
        assert screenshot.filename == "call-log.png"
        assert screenshot.mime_type == "image/png"

        dashboard_html = client.get("/orange-team/").content.decode()
        assert "Team did not answer after 3 attempts." in dashboard_html
        assert "call-log.png" in dashboard_html

        own_dl = client.get(f"/orange-team/screenshots/{screenshot.pk}/")
        assert own_dl.status_code == 200
        assert own_dl["Content-Type"] == "image/png"

        other_client = Client()
        other_client.force_login(other)
        assert other_client.get(f"/orange-team/screenshots/{screenshot.pk}/").status_code == 404

        lead_client = Client()
        lead_client.force_login(lead)
        review_html = lead_client.get("/orange-team/review/").content.decode()
        assert "Team did not answer after 3 attempts." in review_html
        assert "call-log.png" in review_html

        detail_html = lead_client.get(f"/orange-team/checks/{check.pk}/").content.decode()
        assert "Team did not answer after 3 attempts." in detail_html
        assert "call-log.png" in detail_html

        lead_dl = lead_client.get(f"/orange-team/screenshots/{screenshot.pk}/")
        assert lead_dl.status_code == 200

        export_csv = lead_client.get("/orange-team/export/").content.decode()
        assert "Evaluator Notes" in export_csv
        assert "Team did not answer after 3 attempts." in export_csv

        lead_client.post(f"/orange-team/assignments/{assignment.pk}/reject/", {"notes": "Wrong file"})
        replacement = SimpleUploadedFile("updated-log.png", png_bytes, content_type="image/png")
        client.post(
            f"/orange-team/assignments/{assignment.pk}/submit/",
            {
                "evaluator_notes": "Updated notes",
                "screenshots": [replacement],
            },
        )
        assert list(assignment.screenshots.values_list("filename", flat=True)) == ["updated-log.png"]

    def test_cannot_submit_twice(self) -> None:
        user = User.objects.create_user(username="orange1", password="test")
        UserGroups.objects.create(user=user, authentik_id="o1", groups=["WCComps_OrangeTeam"])
        team = Team.objects.create(team_number=1, team_name="T1")
        check = OrangeCheck.objects.create(title="Test", description="Desc")
        assignment = OrangeAssignment.objects.create(orange_check=check, user=user, team=team, status="submitted")
        client = Client()
        client.force_login(User.objects.get(username="orange1"))
        response = client.post(f"/orange-team/assignments/{assignment.pk}/submit/")
        assert response.status_code == 302  # redirects with error message
