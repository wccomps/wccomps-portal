"""Permission integration tests for scoring views."""

import pytest
from django.test import Client
from django.urls import reverse

from team.models import Team

pytestmark = pytest.mark.django_db


class TestLeaderboardPermissions:
    """Test permissions for leaderboard view (/scoring/)."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("leaderboard_page"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    def test_blue_team_denied(self, blue_team_user):
        """Blue Team should not access leaderboard."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 302

    def test_red_team_allowed(self, red_team_user):
        """Red Team should access leaderboard."""
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200

    def test_gold_team_allowed(self, gold_team_user):
        """Gold Team should access leaderboard."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200

    def test_white_team_allowed(self, white_team_user):
        """White Team should access leaderboard."""
        client = Client()
        client.force_login(white_team_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200

    def test_orange_team_allowed(self, orange_team_user):
        """Orange Team should access leaderboard."""
        client = Client()
        client.force_login(orange_team_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200

    def test_ticketing_support_allowed(self, ticketing_support_user):
        """Ticketing Support should access leaderboard."""
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200

    def test_ticketing_admin_allowed(self, ticketing_admin_user):
        """Ticketing Admin should access leaderboard."""
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200

    def test_admin_allowed(self, admin_user):
        """Admin (Gold Team) should access leaderboard."""
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("leaderboard_page"))
        assert response.status_code == 200


class TestScorecardPermissions:
    """Test permissions for scorecard and scorecard PDF views."""

    @pytest.fixture
    def test_team(self):
        return Team.objects.create(team_number=1, team_name="Team 1", is_active=True)

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client, test_team):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("leaderboard_scorecard", args=[test_team.team_number]))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

        pdf_response = unauthenticated_client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 302
        assert "/accounts/" in pdf_response.url or "login" in pdf_response.url

    def test_blue_team_denied(self, blue_team_user, test_team):
        """Blue Team should not access scorecard or PDF export."""
        client = Client()
        client.force_login(blue_team_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 302
        assert client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number])).status_code == 302

    def test_red_team_allowed(self, red_team_user, test_team):
        """Red Team should access scorecard and PDF export."""
        client = Client()
        client.force_login(red_team_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 200
        pdf_response = client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 200
        assert pdf_response["Content-Type"] == "application/pdf"

    def test_gold_team_allowed(self, gold_team_user, test_team):
        """Gold Team should access scorecard and PDF export."""
        client = Client()
        client.force_login(gold_team_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 200
        pdf_response = client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 200
        assert pdf_response["Content-Type"] == "application/pdf"

    def test_white_team_allowed(self, white_team_user, test_team):
        """White Team should access scorecard and PDF export."""
        client = Client()
        client.force_login(white_team_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 200
        pdf_response = client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 200
        assert pdf_response["Content-Type"] == "application/pdf"

    def test_orange_team_allowed(self, orange_team_user, test_team):
        """Orange Team should access scorecard and PDF export."""
        client = Client()
        client.force_login(orange_team_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 200
        pdf_response = client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 200
        assert pdf_response["Content-Type"] == "application/pdf"

    def test_ticketing_support_allowed(self, ticketing_support_user, test_team):
        """Ticketing Support should access scorecard and PDF export."""
        client = Client()
        client.force_login(ticketing_support_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 200
        pdf_response = client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 200
        assert pdf_response["Content-Type"] == "application/pdf"

    def test_ticketing_admin_allowed(self, ticketing_admin_user, test_team):
        """Ticketing Admin should access scorecard and PDF export."""
        client = Client()
        client.force_login(ticketing_admin_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 200
        pdf_response = client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 200
        assert pdf_response["Content-Type"] == "application/pdf"

    def test_admin_allowed(self, admin_user, test_team):
        """Admin (Gold Team) should access scorecard and PDF export."""
        client = Client()
        client.force_login(admin_user)
        assert client.get(reverse("leaderboard_scorecard", args=[test_team.team_number])).status_code == 200
        pdf_response = client.get(reverse("leaderboard_scorecard_pdf", args=[test_team.team_number]))
        assert pdf_response.status_code == 200
        assert pdf_response["Content-Type"] == "application/pdf"


class TestRedTeamFindingsPermissions:
    """Test permissions for Red Team findings page (/scoring/red-team/)."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    def test_blue_team_denied(self, blue_team_user):
        """Blue Team should not access Red Team findings page."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 302

    def test_red_team_allowed(self, red_team_user):
        """Red Team should access Red Team findings page for review."""
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 200

    def test_red_team_uses_findings_view(self, red_team_user):
        """Red Team should access their own findings view."""
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("scoring:red_team_scores"))
        assert response.status_code == 200

    def test_gold_team_allowed(self, gold_team_user):
        """Gold Team should access Red Team findings page."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 200

    def test_white_team_denied(self, white_team_user):
        """White Team should not access Red Team findings page."""
        client = Client()
        client.force_login(white_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 302

    def test_orange_team_denied(self, orange_team_user):
        """Orange Team should not access Red Team findings page."""
        client = Client()
        client.force_login(orange_team_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 302

    def test_ticketing_support_denied(self, ticketing_support_user):
        """Ticketing Support should not access Red Team findings page."""
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 302

    def test_ticketing_admin_denied(self, ticketing_admin_user):
        """Ticketing Admin should not access Red Team findings page."""
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 302

    def test_admin_allowed(self, admin_user):
        """Admin (Gold Team) should access Red Team findings page."""
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("scoring:red_team_findings"))
        assert response.status_code == 200


class TestIncidentSubmissionPermissions:
    """Test permissions for Incident Submission (/scoring/incident/submit/)."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("scoring:submit_incident_report"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    def test_blue_team_denied_without_team(self, blue_team_user):
        """Blue Team user without a team should be denied."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("scoring:submit_incident_report"))
        # Should redirect to leaderboard with error message
        assert response.status_code == 302

    def test_red_team_denied_without_team(self, red_team_user):
        """Red Team should not submit incidents (not a blue team)."""
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("scoring:submit_incident_report"))
        assert response.status_code == 302

    def test_gold_team_allowed_without_team(self, gold_team_user, mock_quotient_client):
        """Gold Team can submit incidents as admin (team selection in form)."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:submit_incident_report"))
        assert response.status_code == 200

    def test_white_team_allowed_without_team(self, white_team_user, mock_quotient_client):
        """White Team can submit incidents as admin (team selection in form)."""
        client = Client()
        client.force_login(white_team_user)
        response = client.get(reverse("scoring:submit_incident_report"))
        assert response.status_code == 200

    def test_orange_team_denied_without_team(self, orange_team_user):
        """Orange Team should not submit incidents (not a blue team)."""
        client = Client()
        client.force_login(orange_team_user)
        response = client.get(reverse("scoring:submit_incident_report"))
        assert response.status_code == 302

    def test_admin_allowed(self, admin_user, mock_quotient_client):
        """Admin (Gold Team) should submit incidents."""
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("scoring:submit_incident_report"))
        assert response.status_code == 200


class TestOrangeTeamRedirectPermissions:
    """Test that old Orange Team Portal redirects to orange_team dashboard."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("scoring:orange_team_redirect"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    def test_authenticated_user_redirects_to_dashboard(self, gold_team_user):
        """Authenticated users are redirected to orange_team dashboard."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:orange_team_redirect"))
        assert response.status_code == 302
        assert response.url == reverse("orange_team:dashboard")


class TestInjectGradingPermissions:
    """Test permissions for Inject Grading (/scoring/injects/)."""

    def test_unauthenticated_redirects_to_login(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    def test_blue_team_denied(self, blue_team_user):
        """Blue Team should not access Inject Grading."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 302

    def test_red_team_denied(self, red_team_user):
        """Red Team should not access Inject Grading."""
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 302

    def test_gold_team_allowed(self, gold_team_user, mock_quotient_client):
        """Gold Team should access Inject Grading."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 200

    def test_white_team_allowed(self, white_team_user, mock_quotient_client):
        """White Team should access Inject Grading."""
        client = Client()
        client.force_login(white_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 200

    def test_orange_team_denied(self, orange_team_user):
        """Orange Team should not access Inject Grading."""
        client = Client()
        client.force_login(orange_team_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 302

    def test_ticketing_support_denied(self, ticketing_support_user):
        """Ticketing Support should not access Inject Grading."""
        client = Client()
        client.force_login(ticketing_support_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 302

    def test_ticketing_admin_denied(self, ticketing_admin_user):
        """Ticketing Admin should not access Inject Grading."""
        client = Client()
        client.force_login(ticketing_admin_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 302

    def test_admin_allowed(self, admin_user, mock_quotient_client):
        """Admin (Gold Team) should access Inject Grading."""
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("scoring:inject_grading"))
        assert response.status_code == 200


class TestExportViewsPermissions:
    """Test permissions for Export views (/scoring/export/)."""

    def test_export_index_unauthenticated_redirects(self, unauthenticated_client):
        """Unauthenticated users should be redirected to login."""
        response = unauthenticated_client.get(reverse("scoring:export_index"))
        assert response.status_code == 302
        assert "/accounts/" in response.url or "login" in response.url

    def test_export_index_blue_team_denied(self, blue_team_user):
        """Blue Team should not access export index."""
        client = Client()
        client.force_login(blue_team_user)
        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 302

    def test_export_index_red_team_denied(self, red_team_user):
        """Red Team should not access export index."""
        client = Client()
        client.force_login(red_team_user)
        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 302

    def test_export_index_gold_team_allowed(self, gold_team_user):
        """Gold Team should access export index."""
        client = Client()
        client.force_login(gold_team_user)
        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 200

    def test_export_index_white_team_denied(self, white_team_user):
        """White Team should not access export index (admin only)."""
        client = Client()
        client.force_login(white_team_user)
        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 302

    def test_export_index_orange_team_denied(self, orange_team_user):
        """Orange Team should not access export index."""
        client = Client()
        client.force_login(orange_team_user)
        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 302

    def test_export_index_admin_allowed(self, admin_user):
        """Admin (Gold Team) should access export index."""
        client = Client()
        client.force_login(admin_user)
        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 200
