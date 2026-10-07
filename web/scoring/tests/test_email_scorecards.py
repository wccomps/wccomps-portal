"""Tests for email scorecard views."""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from core.models import UserGroups
from scoring.models import ScoringTemplate, ServiceScore
from team.models import SchoolInfo, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def gold_user():
    user = User.objects.create_user(username="gold", password="testpass123")
    UserGroups.objects.create(user=user, authentik_id="gold-uid", groups=["WCComps_GoldTeam"])
    return user


@pytest.fixture
def red_user():
    user = User.objects.create_user(username="red", password="testpass123")
    UserGroups.objects.create(user=user, authentik_id="red-uid", groups=["WCComps_RedTeam"])
    return user


@pytest.fixture
def teams():
    return [
        Team.objects.create(team_number=1, team_name="Team 01", is_active=True),
        Team.objects.create(team_number=2, team_name="Team 02", is_active=True),
    ]


@pytest.fixture
def school_infos(teams):
    return [
        SchoolInfo.objects.create(
            team=teams[0],
            school_name="Alpha High",
            contact_email="alpha@example.edu",
            secondary_email="alpha2@example.edu",
        ),
        SchoolInfo.objects.create(
            team=teams[1],
            school_name="Beta Academy",
            contact_email="beta@example.edu",
        ),
    ]


@pytest.fixture
def scoring_template():
    return ScoringTemplate.objects.create(
        service_weight=Decimal("40"),
        inject_weight=Decimal("40"),
        orange_weight=Decimal("20"),
        service_max=Decimal("1000"),
        inject_max=Decimal("500"),
        orange_max=Decimal("100"),
    )


@pytest.fixture
def final_scores(teams, scoring_template):
    """Service points give both teams scoring activity: team 1 ranks first, team 2 second."""
    return [
        ServiceScore.objects.create(team=teams[0], service_points=Decimal("500")),
        ServiceScore.objects.create(team=teams[1], service_points=Decimal("400")),
    ]


class TestEmailScorecardsPermissions:
    """Test that only gold team can access email scorecard views."""

    def test_non_gold_user_cannot_access_bulk_email(self, red_user, final_scores):
        client = Client()
        client.force_login(red_user)
        response = client.get(reverse("scoring:email_scorecards"))
        assert response.status_code == 302

    def test_non_gold_user_cannot_access_single_email(self, red_user, final_scores):
        client = Client()
        client.force_login(red_user)
        response = client.get(reverse("scoring:email_scorecard", args=[1]))
        assert response.status_code == 302

    def test_gold_user_can_access_bulk_email(self, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecards"))
        assert response.status_code == 200

    def test_gold_user_can_access_single_email(self, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecard", args=[1]))
        assert response.status_code == 200


class TestBulkEmailScorecardsGET:
    """Test the bulk email confirmation page."""

    def test_shows_teams_with_emails(self, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecards"))
        content = response.content.decode()
        assert "alpha@example.edu" in content
        assert "beta@example.edu" in content

    def test_shows_teams_without_school_info(self, gold_user, teams, final_scores):
        """Teams without SchoolInfo should show 'No email'."""
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecards"))
        content = response.content.decode()
        assert "No email" in content

    def test_shows_default_message(self, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecards"))
        content = response.content.decode()
        assert "Be careful when hardening your systems" in content
        assert "Password Change Request (PCR)" in content

    def test_redirects_when_no_scores(self, gold_user, teams):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecards"))
        assert response.status_code == 302


# The sends stream from their own thread (core.utils.run_detached), which needs committed rows
@pytest.mark.django_db(transaction=True)
class TestStreamEmailScorecards:
    """Test the streaming bulk email endpoint."""

    def _parse_ndjson(self, response):
        """Parse NDJSON streaming response into list of dicts."""
        content = b"".join(response.streaming_content).decode()
        return [json.loads(line) for line in content.strip().split("\n") if line.strip()]

    @patch("scoring.views.export._send_scorecard_email", return_value=True)
    def test_get_sends_nothing(self, mock_send, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:stream_email_scorecards"))
        assert response.status_code == 405
        mock_send.assert_not_called()

    @patch("scoring.views.export._send_scorecard_email", return_value=True)
    def test_streams_progress_for_each_team(self, mock_send, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.post(reverse("scoring:stream_email_scorecards"))
        assert response["Content-Type"] == "application/x-ndjson"

        messages = self._parse_ndjson(response)
        # 2 progress messages + 1 done message
        assert len(messages) == 3
        assert messages[0]["current"] == 1
        assert messages[1]["current"] == 2
        assert messages[2]["done"] is True
        assert messages[2]["success"] is True
        assert mock_send.call_count == 2

    @patch("scoring.views.export._send_scorecard_email", return_value=True)
    def test_streams_progress_with_custom_message(self, mock_send, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.post(
            reverse("scoring:stream_email_scorecards"),
            {"custom_message": "Custom blast message"},
        )
        assert response["Content-Type"] == "application/x-ndjson"
        messages = self._parse_ndjson(response)
        assert len(messages) == 3
        assert mock_send.call_count == 2
        for call in mock_send.call_args_list:
            email_ctx = call[0][1]
            assert email_ctx["custom_message"] == "Custom blast message"

    @patch("scoring.views.export._send_scorecard_email", return_value=True)
    def test_skips_teams_without_school_info(self, mock_send, gold_user, teams, final_scores):
        """Teams without SchoolInfo should be skipped."""
        client = Client()
        client.force_login(gold_user)
        response = client.post(reverse("scoring:stream_email_scorecards"))

        messages = self._parse_ndjson(response)
        assert len(messages) == 1
        assert messages[0]["done"] is True
        assert messages[0]["success"] is True
        assert mock_send.call_count == 0

    @patch("scoring.views.export._send_scorecard_email", return_value=False)
    def test_reports_failures_in_stream(self, mock_send, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.post(reverse("scoring:stream_email_scorecards"))

        messages = self._parse_ndjson(response)
        progress_msgs = [m for m in messages if not m.get("done")]
        done_msg = [m for m in messages if m.get("done")][0]

        assert all(not m["ok"] for m in progress_msgs)
        assert done_msg["success"] is False
        assert "failed" in done_msg["message"].lower()

    def test_non_gold_user_cannot_stream(self, red_user, final_scores):
        client = Client()
        client.force_login(red_user)
        response = client.post(reverse("scoring:stream_email_scorecards"))
        assert response.status_code == 302


class TestSendScorecardEmail:
    """Test the _send_scorecard_email helper directly."""

    @patch("django.core.mail.EmailMultiAlternatives")
    def test_sends_email_with_correct_recipients(self, mock_email_cls):
        from scoring.views.export import _send_scorecard_email

        mock_email = MagicMock()
        mock_email_cls.return_value = mock_email

        ctx = {"school_name": "Test School"}
        result = _send_scorecard_email(["a@test.com", "b@test.com"], ctx, 1, b"pdf-bytes")

        assert result is True
        mock_email_cls.assert_called_once()
        assert mock_email_cls.call_args[1]["to"] == ["a@test.com", "b@test.com"]
        mock_email.attach_alternative.assert_called_once()
        mock_email.attach.assert_called_once_with("team-01-scorecard.pdf", b"pdf-bytes", "application/pdf")
        mock_email.send.assert_called_once_with(fail_silently=False)

    @patch("django.core.mail.EmailMultiAlternatives")
    def test_sends_email_with_custom_message(self, mock_email_cls):
        from scoring.views.export import _send_scorecard_email

        mock_email = MagicMock()
        mock_email_cls.return_value = mock_email

        ctx = {"school_name": "Test School", "custom_message": "Hardening warning message"}
        result = _send_scorecard_email(["a@test.com"], ctx, 1, b"pdf-bytes")

        assert result is True
        body = mock_email_cls.call_args[1]["body"]
        assert "Hardening warning message" in body
        html_content = mock_email.attach_alternative.call_args[0][0]
        assert "Hardening warning message" in html_content
        assert 'class="advisory"' in html_content

    @patch("django.core.mail.EmailMultiAlternatives")
    def test_returns_false_on_send_failure(self, mock_email_cls):
        from scoring.views.export import _send_scorecard_email

        mock_email = MagicMock()
        mock_email.send.side_effect = Exception("SMTP error")
        mock_email_cls.return_value = mock_email

        ctx = {"school_name": "Test School"}
        result = _send_scorecard_email(["a@test.com"], ctx, 1, b"pdf-bytes")

        assert result is False


class TestSingleEmailScorecardGET:
    """Test the single team email confirmation page."""

    def test_shows_team_and_email_info(self, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecard", args=[1]))
        content = response.content.decode()
        assert "alpha@example.edu" in content
        assert "Alpha High" in content

    def test_shows_default_message_in_form(self, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecard", args=[1]))
        content = response.content.decode()
        assert "Be careful when hardening your systems" in content
        assert "Password Change Request (PCR)" in content

    def test_redirects_when_no_school_info(self, gold_user, teams, final_scores):
        client = Client()
        client.force_login(gold_user)
        response = client.get(reverse("scoring:email_scorecard", args=[1]))
        assert response.status_code == 302


class TestSingleEmailScorecardPOST:
    """Test the single team email POST action."""

    @patch("scoring.views.export._send_scorecard_email", return_value=True)
    def test_sends_email_with_pdf(self, mock_send, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.post(reverse("scoring:email_scorecard", args=[1]))
        assert response.status_code == 302
        assert mock_send.call_count == 1

        call_args = mock_send.call_args[0]
        assert call_args[0] == ["alpha@example.edu", "alpha2@example.edu"]
        assert call_args[2] == 1  # team_number

    @patch("scoring.views.export._send_scorecard_email", return_value=True)
    def test_sends_email_with_custom_message(self, mock_send, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.post(
            reverse("scoring:email_scorecard", args=[1]),
            {"custom_message": "Remember to submit PCRs!"},
        )
        assert response.status_code == 302
        assert mock_send.call_count == 1

        call_args = mock_send.call_args[0]
        email_ctx = call_args[1]
        assert email_ctx["custom_message"] == "Remember to submit PCRs!"

    @patch("scoring.views.export._send_scorecard_email", return_value=False)
    def test_reports_failure(self, mock_send, gold_user, final_scores, school_infos):
        client = Client()
        client.force_login(gold_user)
        response = client.post(reverse("scoring:email_scorecard", args=[1]), follow=True)
        content = response.content.decode()
        assert "Failed" in content
