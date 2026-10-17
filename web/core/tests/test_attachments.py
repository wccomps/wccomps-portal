"""Tests for ticket attachment upload and download functionality."""

from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from core.models import UserGroups
from team.models import Team
from ticketing.models import Ticket, TicketAttachment, TicketCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup_teams() -> tuple[Team, Team]:
    """Create test teams."""
    team1 = Team.objects.create(
        team_name="Team Alpha",
        team_number=1,
        ticket_counter=0,
    )
    team2 = Team.objects.create(
        team_name="Team Beta",
        team_number=2,
        ticket_counter=0,
    )
    return team1, team2


@pytest.fixture
def setup_users_and_auth(setup_teams: tuple[Team, Team]) -> dict[str, Any]:
    """Create test users with authentication and groups."""
    team1, team2 = setup_teams

    # Team 1 user
    team1_user = User.objects.create_user(username="team1_user", password="test123")
    UserGroups.objects.create(user=team1_user, authentik_id="team1_uid", groups=["WCComps_BlueTeam01"])

    # Team 2 user
    team2_user = User.objects.create_user(username="team2_user", password="test123")
    UserGroups.objects.create(user=team2_user, authentik_id="team2_uid", groups=["WCComps_BlueTeam02"])

    # Ops user (ticketing support)
    ops_user = User.objects.create_user(username="ops_user", password="test123")
    UserGroups.objects.create(user=ops_user, authentik_id="ops_uid", groups=["WCComps_Ticketing_Support"])

    return {
        "team1": team1,
        "team2": team2,
        "team1_user": team1_user,
        "team2_user": team2_user,
        "ops_user": ops_user,
    }


@pytest.fixture
def setup_tickets(setup_users_and_auth: dict[str, Any]) -> dict[str, Any]:
    """Create test tickets."""
    data = setup_users_and_auth

    ticket1 = Ticket.objects.create(
        ticket_number="T001-001",
        team=data["team1"],
        category=TicketCategory.objects.get(pk=6),
        title="Test Ticket 1",
        description="Test description",
        status="open",
    )

    ticket2 = Ticket.objects.create(
        ticket_number="T002-001",
        team=data["team2"],
        category=TicketCategory.objects.get(pk=6),
        title="Test Ticket 2",
        description="Test description",
        status="open",
    )

    data["ticket1"] = ticket1
    data["ticket2"] = ticket2
    return data


@pytest.mark.django_db
class TestTeamAttachmentUpload:
    """Test team member attachment upload functionality."""

    def test_upload_success(self, setup_tickets: dict[str, Any]) -> None:
        """Test successful file upload by team member."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        # Create a test file
        file_content = b"Test file content"
        test_file = SimpleUploadedFile("test.txt", file_content, content_type="text/plain")

        response = client.post(
            f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
            {"attachment": test_file},
        )

        # Should redirect to ticket detail
        assert response.status_code == 302
        assert response["Location"] == f"/tickets/{data['ticket1'].ticket_number}/"

        # Check attachment was created
        attachment = TicketAttachment.objects.filter(ticket=data["ticket1"]).first()
        assert attachment is not None
        assert attachment.filename == "test.txt"
        assert attachment.mime_type == "text/plain"
        assert bytes(attachment.file_data) == file_content
        assert attachment.uploaded_by == "team1_user"

    def test_upload_file_too_large(self, setup_tickets: dict[str, Any]) -> None:
        """Test upload fails when file exceeds 10MB limit."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        # Create file larger than 10MB
        large_content = b"x" * (11 * 1024 * 1024)  # 11MB
        large_file = SimpleUploadedFile("large.bin", large_content)

        response = client.post(
            f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
            {"attachment": large_file},
        )

        assert response.status_code == 400
        assert b"File too large" in response.content

        # No attachment should be created
        assert TicketAttachment.objects.filter(ticket=data["ticket1"]).count() == 0

    def test_upload_no_file_provided(self, setup_tickets: dict[str, Any]) -> None:
        """Test upload fails when no file is provided."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        response = client.post(
            f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
            {},
        )

        assert response.status_code == 400
        assert b"No file provided" in response.content

    def test_upload_wrong_team_denied(self, setup_tickets: dict[str, Any]) -> None:
        """Test team member cannot upload to another team's ticket."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        # Try to upload to team2's ticket
        test_file = SimpleUploadedFile("test.txt", b"content", content_type="text/plain")

        response = client.post(
            f"/tickets/{data['ticket2'].ticket_number}/attachment/upload/",
            {"attachment": test_file},
        )

        assert response.status_code == 404
        assert TicketAttachment.objects.filter(ticket=data["ticket2"]).count() == 0

    def test_upload_method_not_allowed(self, setup_tickets: dict[str, Any]) -> None:
        """Test GET request to upload endpoint returns 405."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/")

        assert response.status_code == 405

    def test_upload_unauthenticated_redirects(self, setup_tickets: dict[str, Any]) -> None:
        """Test unauthenticated user is redirected to login."""
        data = setup_tickets
        client = Client()

        test_file = SimpleUploadedFile("test.txt", b"content")
        response = client.post(
            f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
            {"attachment": test_file},
        )

        # Should redirect to login
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]


@pytest.mark.django_db
class TestTeamAttachmentDownload:
    """Test team member attachment download functionality."""

    def test_download_success(self, setup_tickets: dict[str, Any]) -> None:
        """Test successful file download by team member."""
        data = setup_tickets

        # Create an attachment
        file_content = b"Test file content"
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=file_content,
            filename="test.pdf",
            mime_type="application/pdf",
            uploaded_by="team1_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{attachment.id}/")

        assert response.status_code == 200
        # Served by its bytes (not a PDF), not the stored type
        assert response["Content-Type"] == "application/octet-stream"
        assert b"attachment" in response["Content-Disposition"].encode()
        assert b"test.pdf" in response["Content-Disposition"].encode()
        assert response.content == file_content

    def test_download_wrong_team_denied(self, setup_tickets: dict[str, Any]) -> None:
        """Test team member cannot download another team's attachment."""
        data = setup_tickets

        # Create attachment for team2
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket2"],
            file_data=b"Secret content",
            filename="secret.txt",
            mime_type="text/plain",
            uploaded_by="team2_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        # Try to download team2's attachment
        response = client.get(f"/tickets/{data['ticket2'].ticket_number}/attachment/{attachment.id}/")

        assert response.status_code == 404

    def test_download_attachment_not_found(self, setup_tickets: dict[str, Any]) -> None:
        """Test download fails when attachment doesn't exist."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/99999/")

        assert response.status_code == 404

    def test_download_special_characters_in_filename(self, setup_tickets: dict[str, Any]) -> None:
        """Test download handles filenames with special characters safely."""
        data = setup_tickets

        # Create attachment with special chars in filename
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=b"Content",
            filename='my "file" name.txt',
            mime_type="text/plain",
            uploaded_by="team1_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{attachment.id}/")

        assert response.status_code == 200
        # Django's content_disposition_header should properly escape the filename
        assert b"attachment" in response["Content-Disposition"].encode()


@pytest.mark.django_db
class TestOpsAttachmentUpload:
    """Test ops team attachment upload functionality."""

    def test_ops_upload_success(self, setup_tickets: dict[str, Any]) -> None:
        """Test successful file upload by ops team member."""
        data = setup_tickets
        client = Client()
        client.force_login(data["ops_user"])

        file_content = b"Ops uploaded file"
        test_file = SimpleUploadedFile("ops_file.txt", file_content, content_type="text/plain")

        response = client.post(
            f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
            {"attachment": test_file},
        )

        # Should redirect to ops ticket detail
        assert response.status_code == 302
        assert f"/tickets/{data['ticket1'].ticket_number}/" in response["Location"]

        # Check attachment was created
        attachment = TicketAttachment.objects.filter(ticket=data["ticket1"]).first()
        assert attachment is not None
        assert attachment.filename == "ops_file.txt"
        assert attachment.uploaded_by == "ops_user"

    def test_ops_upload_any_team_ticket(self, setup_tickets: dict[str, Any]) -> None:
        """Test ops team can upload to any team's ticket."""
        data = setup_tickets
        client = Client()
        client.force_login(data["ops_user"])

        # Upload to team1's ticket
        test_file1 = SimpleUploadedFile("file1.txt", b"content1")
        response1 = client.post(
            f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
            {"attachment": test_file1},
        )
        assert response1.status_code == 302

        # Upload to team2's ticket
        test_file2 = SimpleUploadedFile("file2.txt", b"content2")
        response2 = client.post(
            f"/tickets/{data['ticket2'].ticket_number}/attachment/upload/",
            {"attachment": test_file2},
        )
        assert response2.status_code == 302

        assert TicketAttachment.objects.filter(ticket=data["ticket1"]).count() == 1
        assert TicketAttachment.objects.filter(ticket=data["ticket2"]).count() == 1

    def test_ops_upload_denied_for_other_team(self, setup_tickets: dict[str, Any]) -> None:
        """Test regular team member cannot upload to another team's ticket."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        test_file = SimpleUploadedFile("test.txt", b"content")
        response = client.post(
            f"/tickets/{data['ticket2'].ticket_number}/attachment/upload/",
            {"attachment": test_file},
        )

        assert response.status_code == 404

    def test_ops_upload_ticket_not_found(self, setup_tickets: dict[str, Any]) -> None:
        """Test upload fails when ticket doesn't exist."""
        data = setup_tickets
        client = Client()
        client.force_login(data["ops_user"])

        test_file = SimpleUploadedFile("test.txt", b"content")
        response = client.post(
            "/tickets/T999-999/attachment/upload/",
            {"attachment": test_file},
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestOpsAttachmentDownload:
    """Test ops team attachment download functionality."""

    def test_ops_download_success(self, setup_tickets: dict[str, Any]) -> None:
        """Test successful file download by ops team member."""
        data = setup_tickets

        file_content = b"Ops downloadable file"
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=file_content,
            filename="ops_download.pdf",
            mime_type="application/pdf",
            uploaded_by="team1_user",
        )

        client = Client()
        client.force_login(data["ops_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{attachment.id}/")

        assert response.status_code == 200
        # Served by its bytes (not a PDF), not the stored type
        assert response["Content-Type"] == "application/octet-stream"
        assert b"ops_download.pdf" in response["Content-Disposition"].encode()
        assert response.content == file_content

    def test_ops_download_any_team_attachment(self, setup_tickets: dict[str, Any]) -> None:
        """Test ops team can download attachments from any team's ticket."""
        data = setup_tickets

        # Create attachments for both teams
        att1 = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=b"Team1 file",
            filename="team1.txt",
            mime_type="text/plain",
            uploaded_by="team1_user",
        )

        att2 = TicketAttachment.objects.create(
            ticket=data["ticket2"],
            file_data=b"Team2 file",
            filename="team2.txt",
            mime_type="text/plain",
            uploaded_by="team2_user",
        )

        client = Client()
        client.force_login(data["ops_user"])

        # Download from team1
        response1 = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{att1.id}/")
        assert response1.status_code == 200
        assert response1.content == b"Team1 file"

        # Download from team2
        response2 = client.get(f"/tickets/{data['ticket2'].ticket_number}/attachment/{att2.id}/")
        assert response2.status_code == 200
        assert response2.content == b"Team2 file"

    def test_ops_download_denied_for_other_team(self, setup_tickets: dict[str, Any]) -> None:
        """Test regular team member cannot download another team's attachment."""
        data = setup_tickets

        attachment = TicketAttachment.objects.create(
            ticket=data["ticket2"],
            file_data=b"Content",
            filename="file.txt",
            mime_type="text/plain",
            uploaded_by="ops_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        response = client.get(f"/tickets/{data['ticket2'].ticket_number}/attachment/{attachment.id}/")

        assert response.status_code == 404

    def test_ops_download_attachment_not_found(self, setup_tickets: dict[str, Any]) -> None:
        """Test download fails when attachment doesn't exist."""
        data = setup_tickets
        client = Client()
        client.force_login(data["ops_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/99999/")

        assert response.status_code == 404


@pytest.mark.django_db
class TestAttachmentSecurity:
    """Test security aspects of attachment handling."""

    def test_filename_with_newlines_sanitized(self, setup_tickets: dict[str, Any]) -> None:
        """Test that filenames with newlines don't cause header injection."""
        data = setup_tickets

        # Create attachment with malicious filename
        malicious_filename = "test.pdf\r\nX-Evil-Header: malicious"
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=b"Content",
            filename=malicious_filename,
            mime_type="application/pdf",
            uploaded_by="team1_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        # This should raise BadHeaderError from Django, resulting in 500
        # or the content_disposition_header function should sanitize it
        try:
            response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{attachment.id}/")
            # If it doesn't error, check that headers are safe
            disposition = response.get("Content-Disposition", "")
            # Should not contain literal newlines
            assert "\r" not in disposition
            assert "\n" not in disposition
        except Exception as e:
            # BadHeaderError is expected for malicious headers
            assert "BadHeaderError" in str(type(e)) or "newline" in str(e).lower()

    def test_content_disposition_always_attachment(self, setup_tickets: dict[str, Any]) -> None:
        """Test that Content-Disposition is always 'attachment' to prevent execution."""
        data = setup_tickets

        # Create HTML file (potential XSS vector)
        html_content = b"<script>alert('XSS')</script>"
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=html_content,
            filename="evil.html",
            mime_type="text/html",
            uploaded_by="team1_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{attachment.id}/")

        assert response.status_code == 200
        # Content-Disposition should force download, not inline rendering
        assert b"attachment" in response["Content-Disposition"].encode()
        # The HTML shouldn't be executed by browser due to attachment disposition

    def test_large_filename_handled(self, setup_tickets: dict[str, Any]) -> None:
        """Test that very long filenames are handled properly."""
        data = setup_tickets

        # Create attachment with very long filename (255 chars is DB limit)
        long_filename = "a" * 255 + ".txt"
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=b"Content",
            filename=long_filename[:255],  # DB will truncate
            mime_type="text/plain",
            uploaded_by="team1_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{attachment.id}/")

        assert response.status_code == 200
        # Should still work despite long filename

    def test_directory_traversal_in_upload_filename(self, setup_tickets: dict[str, Any]) -> None:
        """Test that filenames with directory traversal characters are sanitized."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        # Attempt various directory traversal attacks
        # Django's UploadedFile.name strips directory components automatically
        traversal_tests = [
            ("../../etc/passwd", "passwd"),  # (input, expected_stored_name)
            ("../../../web/settings.py", "settings.py"),
            ("..\\..\\windows\\system32\\config\\sam", "sam"),
            ("/etc/passwd", "passwd"),
            ("C:\\Windows\\System32\\config\\sam", "sam"),
            ("./../sensitive_file.txt", "sensitive_file.txt"),
        ]

        for malicious_filename, expected_basename in traversal_tests:
            file_content = b"Malicious content"
            test_file = SimpleUploadedFile(malicious_filename, file_content, content_type="text/plain")

            response = client.post(
                f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
                {"attachment": test_file},
            )

            # Upload should succeed
            assert response.status_code == 302

            # Verify Django stripped the path and only stored the basename
            # This prevents directory traversal attacks
            attachment = TicketAttachment.objects.filter(ticket=data["ticket1"], filename=expected_basename).first()
            assert attachment is not None, (
                f"Expected basename '{expected_basename}' not found for input '{malicious_filename}'"
            )
            assert attachment.filename == expected_basename
            assert bytes(attachment.file_data) == file_content

            # Clean up for next iteration
            attachment.delete()

    def test_directory_traversal_cannot_read_arbitrary_files(self, setup_tickets: dict[str, Any]) -> None:
        """Test that we can't use attachment download to read arbitrary system files."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        # Create a normal attachment
        TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=b"Safe content",
            filename="safe.txt",
            mime_type="text/plain",
            uploaded_by="team1_user",
        )

        # Try to access with various malicious attachment IDs
        # (These should all fail because the ID doesn't exist or doesn't belong to the team)
        malicious_attempts = [
            f"/tickets/{data['ticket1'].ticket_number}/attachment/../../../etc/passwd",
            f"/tickets/{data['ticket1'].ticket_number}/attachment/../../settings.py",
            f"/tickets/{data['ticket1'].ticket_number}/attachment/999999999",  # Non-existent ID
        ]

        for malicious_path in malicious_attempts:
            response = client.get(malicious_path, follow=False)
            # Should either be 404 (not found), 301 redirect (URL normalization), or other error
            # Should NOT return actual file contents from the filesystem
            assert response.status_code in [301, 404, 400, 500]

            # If it's a redirect, verify it doesn't redirect to a file read
            if response.status_code == 301:
                # Follow the redirect and ensure it still fails
                response_final = client.get(malicious_path, follow=True)
                # Final response should be 404 for non-existent attachment
                assert response_final.status_code == 404
                # Content should not contain filesystem data
                assert b"/etc/passwd" not in response_final.content
                assert b"root:" not in response_final.content

    def test_path_traversal_in_stored_filename_safe_on_download(self, setup_tickets: dict[str, Any]) -> None:
        """Test that even if we store a filename with path traversal, download is safe."""
        data = setup_tickets

        # Directly create attachment with traversal in filename
        # (simulating if somehow such data got into DB)
        traversal_filename = "../../sensitive.txt"
        file_content = b"This should be downloaded safely"
        attachment = TicketAttachment.objects.create(
            ticket=data["ticket1"],
            file_data=file_content,
            filename=traversal_filename,
            mime_type="text/plain",
            uploaded_by="team1_user",
        )

        client = Client()
        client.force_login(data["team1_user"])

        # Download the attachment
        response = client.get(f"/tickets/{data['ticket1'].ticket_number}/attachment/{attachment.id}/")

        # Should succeed - we're serving from database, not filesystem
        assert response.status_code == 200
        assert response.content == file_content

        # The Content-Disposition header should handle the filename safely
        # Django's content_disposition_header should properly encode it
        content_disp = response.get("Content-Disposition", "")
        assert "attachment" in content_disp
        # The filename should be present but safely encoded
        # The traversal characters should not cause the browser to save to a different directory

    def test_absolute_path_in_upload_filename(self, setup_tickets: dict[str, Any]) -> None:
        """Test that absolute paths in filenames are sanitized to basename only."""
        data = setup_tickets
        client = Client()
        client.force_login(data["team1_user"])

        # Try uploading with absolute path
        # Django strips the path and keeps only the basename
        absolute_path_tests = [
            ("/var/www/html/index.html", "index.html"),  # (input, expected_basename)
            ("/etc/shadow", "shadow"),
            ("C:\\inetpub\\wwwroot\\index.html", "index.html"),
        ]

        for abs_path, expected_basename in absolute_path_tests:
            test_file = SimpleUploadedFile(abs_path, b"Overwrite attempt", content_type="text/html")

            response = client.post(
                f"/tickets/{data['ticket1'].ticket_number}/attachment/upload/",
                {"attachment": test_file},
            )

            # Should succeed
            assert response.status_code == 302

            # Verify Django stripped the absolute path and only stored the basename
            # This prevents file overwrite attacks on the filesystem
            attachment = TicketAttachment.objects.filter(ticket=data["ticket1"], filename=expected_basename).first()
            assert attachment is not None, f"Expected basename '{expected_basename}' not found for input '{abs_path}'"
            assert attachment.filename == expected_basename
            # File data is safely stored in database, not written to the absolute path
            assert bytes(attachment.file_data) == b"Overwrite attempt"

            # Clean up for next iteration
            attachment.delete()
