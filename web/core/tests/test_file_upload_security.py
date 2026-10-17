"""
Security tests for file upload functionality.

Files are stored in PostgreSQL as BLOBs and served as attachments, except images and PDFs
requested with ?inline=1 (web/ticketing/views/attachments.py).
Filename sanitization is NOT needed because:
- Django's BadHeaderError blocks header injection
- No filesystem access (files stored in DB)
- Modern browsers ignore path traversal in downloads

REAL security concerns:
- Authorization bypass (cross-team access)
- File size limits (resource exhaustion)
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from core.tests.roles import make_user
from team.models import Team
from ticketing.models import Ticket, TicketAttachment, TicketCategory


def _member_of(team: Team) -> Client:
    """A client signed in as a member of the team (it holds the team's Authentik group)."""
    client = Client()
    client.force_login(make_user(f"member{team.team_number}", [team.authentik_group]))
    return client


@pytest.mark.django_db
class TestFileUploadConstraints:
    """Test file upload constraints."""

    @pytest.fixture
    def setup_data(self):
        """Create test team and ticket."""
        team = Team.objects.create(
            team_number=1,
            team_name="Test Team",
            authentik_group="WCComps_BlueTeam01",
        )
        ticket = Ticket.objects.create(
            ticket_number="T001-001",
            team=team,
            category=TicketCategory.objects.get(pk=6),
            title="Test Ticket",
            status="open",
        )
        return team, ticket

    def test_file_size_limit_enforced(self, setup_data):
        """File size limit (10MB) is enforced."""
        team, ticket = setup_data

        large_content = b"A" * (11 * 1024 * 1024)
        uploaded_file = SimpleUploadedFile(name="large.txt", content=large_content)

        response = _member_of(team).post(
            f"/tickets/{ticket.ticket_number}/attachment/upload/", {"attachment": uploaded_file}
        )

        assert response.status_code == 400
        assert b"too large" in response.content.lower() or b"size" in response.content.lower()


@pytest.mark.django_db
class TestFileUploadAuthorizationBypass:
    """Test cross-team access (REAL security concern)."""

    @pytest.fixture
    def setup_teams(self):
        """Create two teams with tickets."""
        team1 = Team.objects.create(
            team_number=1,
            team_name="Team 1",
            authentik_group="WCComps_BlueTeam01",
        )
        team2 = Team.objects.create(
            team_number=2,
            team_name="Team 2",
            authentik_group="WCComps_BlueTeam02",
        )

        ticket1 = Ticket.objects.create(
            ticket_number="T001-001",
            team=team1,
            category=TicketCategory.objects.get(pk=6),
            title="Team 1 Ticket",
        )
        ticket2 = Ticket.objects.create(
            ticket_number="T002-001",
            team=team2,
            category=TicketCategory.objects.get(pk=6),
            title="Team 2 Ticket",
        )

        attachment1 = TicketAttachment.objects.create(
            ticket=ticket1,
            filename="team1_secret.txt",
            file_data=b"Team 1 secret data",
            mime_type="text/plain",
            uploaded_by="team1user",
        )

        return team1, team2, ticket1, ticket2, attachment1

    def test_cannot_download_other_team_attachment(self, setup_teams):
        """Team 2 user cannot download Team 1's attachment."""
        team1, team2, ticket1, ticket2, attachment1 = setup_teams

        response = _member_of(team2).get(f"/tickets/{ticket1.ticket_number}/attachment/{attachment1.id}/")

        assert response.status_code == 404

    def test_cannot_upload_to_other_team_ticket(self, setup_teams):
        """Team 2 user cannot upload to Team 1's ticket."""
        team1, team2, ticket1, ticket2, attachment1 = setup_teams

        uploaded_file = SimpleUploadedFile(
            name="malicious.txt",
            content=b"trying to upload to wrong team",
        )

        response = _member_of(team2).post(
            f"/tickets/{ticket1.ticket_number}/attachment/upload/", {"attachment": uploaded_file}
        )

        assert response.status_code == 404
        assert not TicketAttachment.objects.filter(ticket=ticket1, filename="malicious.txt").exists()
