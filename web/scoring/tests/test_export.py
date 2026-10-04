"""Tests for export functionality."""

import csv
import json
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import UserGroups
from scoring.models import (
    IncidentReport,
    InjectScore,
    OrangeTeamScore,
    RedTeamScore,
    ScoringTemplate,
    ServiceScore,
)
from team.models import Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_user():
    """Create an admin user."""
    user = User.objects.create_user(
        username="admin",
        email="admin@example.com",
        password="admin123",
    )
    UserGroups.objects.create(user=user, authentik_id="admin-uid", groups=["WCComps_GoldTeam"])
    return user


@pytest.fixture
def regular_user():
    """Create a regular non-admin user."""
    return User.objects.create_user(
        username="regular",
        email="regular@example.com",
        password="regular123",
    )


@pytest.fixture
def test_teams():
    """Create test teams."""
    teams = []
    for i in range(1, 4):
        team = Team.objects.create(
            team_name=f"Team {i}",
            team_number=i,
            is_active=True,
        )
        teams.append(team)
    return teams


@pytest.fixture
def red_team_findings(test_teams, admin_user):
    """Create red team findings for testing."""
    findings = []

    # Finding 1: Approved, affecting teams 1 and 2
    finding1 = RedTeamScore.objects.create(
        attack_vector="SQL Injection in login form",
        source_ip="10.0.0.1",
        destination_ip_template="10.100.1X.22",
        affected_boxes=["WebServer"],
        affected_service="HTTP",
        universally_attempted=True,
        persistence_established=False,
        points_per_team=Decimal("50.00"),
        is_approved=True,
        approved_at=timezone.now(),
        approved_by=admin_user,
        submitted_by=admin_user,
        notes="Critical vulnerability",
    )
    finding1.affected_teams.set([test_teams[0], test_teams[1]])
    findings.append(finding1)

    # Finding 2: Not approved, affecting team 3
    finding2 = RedTeamScore.objects.create(
        attack_vector="XSS in comments",
        source_ip="10.0.0.2",
        destination_ip_template="10.100.1X.80",
        affected_boxes=["AppServer"],
        affected_service="HTTPS",
        universally_attempted=False,
        persistence_established=True,
        points_per_team=Decimal("25.00"),
        is_approved=False,
        submitted_by=admin_user,
        notes="Needs review",
    )
    finding2.affected_teams.set([test_teams[2]])
    findings.append(finding2)

    return findings


@pytest.fixture
def incident_reports(test_teams, admin_user, red_team_findings):
    """Create incident reports for testing."""
    incidents = []

    # Incident 1: Reviewed, matched to finding
    incident1 = IncidentReport.objects.create(
        team=test_teams[0],
        attack_description="Detected SQL injection attempt on login page",
        source_ip="10.0.0.1",
        destination_ip="10.100.11.22",
        affected_boxes=["WebServer"],
        affected_service="HTTP",
        attack_detected_at=timezone.now(),
        attack_mitigated=True,
        is_approved=True,
        matched_to_red_score=red_team_findings[0],
        points_returned=Decimal("30.00"),
        submitted_by=admin_user,
        approved_by=admin_user,
        approved_at=timezone.now(),
        evidence_notes="Found in access logs",
    )
    incidents.append(incident1)

    # Incident 2: Not reviewed
    incident2 = IncidentReport.objects.create(
        team=test_teams[1],
        attack_description="Suspicious network traffic detected",
        source_ip="10.0.0.5",
        affected_boxes=["Database"],
        affected_service="PostgreSQL",
        attack_detected_at=timezone.now(),
        attack_mitigated=False,
        is_approved=False,
        points_returned=Decimal("0.00"),
        submitted_by=admin_user,
    )
    incidents.append(incident2)

    return incidents


@pytest.fixture
def orange_adjustments(test_teams, admin_user):
    """Create orange team checks for testing."""
    adjustments = []

    # Adjustment 1: Approved bonus
    adj1 = OrangeTeamScore.objects.create(
        team=test_teams[0],
        description="Excellent customer service during incident response",
        points_awarded=Decimal("10.00"),
        is_approved=True,
        approved_at=timezone.now(),
        approved_by=admin_user,
        submitted_by=admin_user,
    )
    adjustments.append(adj1)

    # Adjustment 2: Not approved penalty
    adj2 = OrangeTeamScore.objects.create(
        team=test_teams[1],
        description="Unprofessional communication",
        points_awarded=Decimal("-5.00"),
        is_approved=False,
        submitted_by=admin_user,
    )
    adjustments.append(adj2)

    return adjustments


@pytest.fixture
def inject_grades(test_teams, admin_user):
    """Create inject grades for testing."""
    grades = []

    # Grade 1: Approved
    grade1 = InjectScore.objects.create(
        team=test_teams[0],
        inject_id="INJ-001",
        inject_name="Incident Response Plan",
        max_points=Decimal("100.00"),
        points_awarded=Decimal("85.00"),
        is_approved=True,
        approved_at=timezone.now(),
        approved_by=admin_user,
        graded_by=admin_user,
        notes="Well documented plan",
    )
    grades.append(grade1)

    # Grade 2: Not approved
    grade2 = InjectScore.objects.create(
        team=test_teams[1],
        inject_id="INJ-001",
        inject_name="Incident Response Plan",
        max_points=Decimal("100.00"),
        points_awarded=Decimal("70.00"),
        is_approved=False,
        graded_by=admin_user,
        notes="Missing key components",
    )
    grades.append(grade2)

    return grades


@pytest.fixture
def final_scores(test_teams):
    """Service scores that rank Team 1 over Team 2 under 1:1 modifiers; Team 3 has no activity."""
    # Maxes proportional to weights -> every derived modifier is exactly 1 (raw == scaled).
    ScoringTemplate.objects.create(service_max=Decimal("40"), inject_max=Decimal("40"), orange_max=Decimal("20"))
    ServiceScore.objects.create(team=test_teams[0], service_points=Decimal("500.00"), sla_violations=Decimal("-10"))
    ServiceScore.objects.create(team=test_teams[1], service_points=Decimal("450.00"))


class TestExportPermissions:
    """Test export endpoint permissions."""

    def test_non_admin_user_cannot_access_red_findings_export(self, regular_user):
        """Non-admin users should get 403 for red findings export."""
        client = Client()
        client.force_login(regular_user)

        response = client.get(reverse("scoring:export_red_scores"))
        assert response.status_code == 302  # Redirect to login/forbidden

    def test_non_admin_user_cannot_access_incidents_export(self, regular_user):
        """Non-admin users should get 403 for incidents export."""
        client = Client()
        client.force_login(regular_user)

        response = client.get(reverse("scoring:export_incidents"))
        assert response.status_code == 302

    def test_non_admin_user_cannot_access_orange_adjustments_export(self, regular_user):
        """Non-admin users should get 403 for orange checks export."""
        client = Client()
        client.force_login(regular_user)

        response = client.get(reverse("scoring:export_orange_adjustments"))
        assert response.status_code == 302

    def test_non_admin_user_cannot_access_inject_grades_export(self, regular_user):
        """Non-admin users should get 403 for inject grades export."""
        client = Client()
        client.force_login(regular_user)

        response = client.get(reverse("scoring:export_inject_grades"))
        assert response.status_code == 302

    def test_non_admin_user_cannot_access_final_scores_export(self, regular_user):
        """Non-admin users should get 403 for final scores export."""
        client = Client()
        client.force_login(regular_user)

        response = client.get(reverse("scoring:export_final_scores"))
        assert response.status_code == 302

    def test_admin_user_can_access_all_exports(self, admin_user):
        """Admin users should be able to access all export endpoints."""
        client = Client()
        client.force_login(admin_user)

        endpoints = [
            "scoring:export_red_scores",
            "scoring:export_incidents",
            "scoring:export_orange_adjustments",
            "scoring:export_inject_grades",
            "scoring:export_final_scores",
        ]

        for endpoint in endpoints:
            response = client.get(reverse(endpoint))
            assert response.status_code == 200, f"Failed to access {endpoint}"


class TestRedFindingsExport:
    """Test red team findings export."""

    def test_csv_export_has_correct_content_type(self, admin_user, red_team_findings):
        """CSV export should have text/csv content type."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "csv"})
        assert response["Content-Type"] == "text/csv"

    def test_csv_export_has_correct_filename(self, admin_user, red_team_findings):
        """CSV export should have correct filename."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "csv"})
        assert 'filename="red_findings.csv"' in response["Content-Disposition"]

    def test_csv_export_contains_all_required_headers(self, admin_user, red_team_findings):
        """CSV export should contain all required column headers."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.reader(StringIO(content))
        headers = next(reader)

        required_headers = [
            "ID",
            "Attack Vector",
            "Source IP",
            "Destination IP Template",
            "Affected Boxes",
            "Affected Service",
            "Affected Teams",
            "Points Per Team",
            "Universally Attempted",
            "Persistence Established",
            "Approved",
            "Approved By",
            "Approved At",
            "Submitted By",
            "Created At",
        ]

        for header in required_headers:
            assert header in headers, f"Missing header: {header}"

    def test_csv_export_data_matches_database(self, admin_user, red_team_findings):
        """CSV export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)

        assert len(rows) == 2, "Should have 2 findings"

        # Find SQL injection finding
        sql_finding = next((r for r in rows if "SQL Injection" in r["Attack Vector"]), None)
        assert sql_finding is not None, "SQL Injection finding not found"
        assert sql_finding["Source IP"] == "10.0.0.1"
        assert sql_finding["Affected Boxes"] == "WebServer"
        assert sql_finding["Approved"] == "True"
        assert "Team 1" in sql_finding["Affected Teams"]
        assert "Team 2" in sql_finding["Affected Teams"]

    def test_csv_export_empty_data_produces_header_only(self, admin_user):
        """CSV export with no data should produce header row only."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.reader(StringIO(content))
        rows = list(reader)

        assert len(rows) == 1, "Should have header row only"
        assert rows[0][0] == "ID"

    def test_json_export_has_correct_content_type(self, admin_user, red_team_findings):
        """JSON export should have application/json content type."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "json"})
        assert response["Content-Type"] == "application/json"

    def test_json_export_has_correct_filename(self, admin_user, red_team_findings):
        """JSON export should have correct filename."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "json"})
        assert 'filename="red_findings.json"' in response["Content-Disposition"]

    def test_json_export_is_valid_json(self, admin_user, red_team_findings):
        """JSON export should produce valid JSON."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "json"})
        data = json.loads(response.content)

        assert "red_findings" in data
        assert isinstance(data["red_findings"], list)

    def test_json_export_contains_all_required_fields(self, admin_user, red_team_findings):
        """JSON export should contain all required fields."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "json"})
        data = json.loads(response.content)
        findings = data["red_findings"]

        assert len(findings) == 2

        required_fields = [
            "id",
            "attack_vector",
            "source_ip",
            "destination_ip_template",
            "affected_boxes",
            "affected_service",
            "affected_teams",
            "points_per_team",
            "universally_attempted",
            "persistence_established",
            "is_approved",
            "approved_by",
            "approved_at",
            "submitted_by",
            "created_at",
        ]

        for field in required_fields:
            assert field in findings[0], f"Missing field: {field}"

    def test_json_export_data_matches_database(self, admin_user, red_team_findings):
        """JSON export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"), {"format": "json"})
        data = json.loads(response.content)
        findings = data["red_findings"]

        # Find SQL injection finding
        sql_finding = next((f for f in findings if "SQL Injection" in f["attack_vector"]), None)
        assert sql_finding is not None, "SQL Injection finding not found"
        assert sql_finding["source_ip"] == "10.0.0.1"
        assert sql_finding["affected_boxes"] == ["WebServer"]
        assert sql_finding["is_approved"] is True
        assert len(sql_finding["affected_teams"]) == 2

    def test_default_format_is_csv(self, admin_user, red_team_findings):
        """Export without format parameter should default to CSV."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_red_scores"))
        assert response["Content-Type"] == "text/csv"


class TestIncidentsExport:
    """Test incident reports export."""

    def test_csv_export_contains_all_required_headers(self, admin_user, incident_reports):
        """CSV export should contain all required headers."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_incidents"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.reader(StringIO(content))
        headers = next(reader)

        required_headers = [
            "ID",
            "Team",
            "Attack Description",
            "Source IP",
            "Destination IP",
            "Affected Boxes",
            "Affected Service",
            "Attack Detected At",
            "Attack Mitigated",
            "Points Returned",
            "Reviewed",
            "Matched Finding ID",
            "Reviewed By",
            "Reviewed At",
            "Submitted By",
            "Created At",
        ]

        for header in required_headers:
            assert header in headers, f"Missing header: {header}"

    def test_csv_export_data_matches_database(self, admin_user, incident_reports):
        """CSV export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_incidents"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)

        assert len(rows) == 2

        # Find SQL injection incident
        sql_incident = next((r for r in rows if "SQL injection" in r["Attack Description"]), None)
        assert sql_incident is not None, "SQL injection incident not found"
        assert sql_incident["Source IP"] == "10.0.0.1"
        assert sql_incident["Reviewed"] == "True"
        assert sql_incident["Points Returned"] == "30.00"

    def test_json_export_contains_all_required_fields(self, admin_user, incident_reports):
        """JSON export should contain all required fields."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_incidents"), {"format": "json"})
        data = json.loads(response.content)
        incidents = data["incidents"]

        assert len(incidents) == 2

        required_fields = [
            "id",
            "team",
            "team_number",
            "attack_description",
            "source_ip",
            "destination_ip",
            "affected_boxes",
            "affected_service",
            "attack_detected_at",
            "attack_mitigated",
            "points_returned",
            "is_approved",
            "matched_to_red_score_id",
            "approved_by",
            "approved_at",
            "submitted_by",
            "created_at",
        ]

        for field in required_fields:
            assert field in incidents[0], f"Missing field: {field}"

    def test_json_export_data_matches_database(self, admin_user, incident_reports):
        """JSON export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_incidents"), {"format": "json"})
        data = json.loads(response.content)
        incidents = data["incidents"]

        # Find SQL injection incident
        sql_incident = next((i for i in incidents if "SQL injection" in i["attack_description"]), None)
        assert sql_incident is not None, "SQL injection incident not found"
        assert sql_incident["source_ip"] == "10.0.0.1"
        assert sql_incident["is_approved"] is True
        assert sql_incident["points_returned"] == "30.00"


class TestOrangeAdjustmentsExport:
    """Test orange team checks export."""

    def test_csv_export_contains_all_required_headers(self, admin_user, orange_adjustments):
        """CSV export should contain all required headers."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_orange_adjustments"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.reader(StringIO(content))
        headers = next(reader)

        required_headers = [
            "ID",
            "Team",
            "Description",
            "Points",
            "Approved",
            "Approved By",
            "Approved At",
            "Submitted By",
            "Created At",
        ]

        for header in required_headers:
            assert header in headers, f"Missing header: {header}"

    def test_csv_export_data_matches_database(self, admin_user, orange_adjustments):
        """CSV export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_orange_adjustments"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)

        assert len(rows) == 2

        # Find approved bonus
        bonus = next((r for r in rows if "Excellent customer service" in r["Description"]), None)
        assert bonus is not None, "Approved bonus not found"
        assert bonus["Points"] == "10.00"
        assert bonus["Approved"] == "True"

    def test_json_export_contains_all_required_fields(self, admin_user, orange_adjustments):
        """JSON export should contain all required fields."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_orange_adjustments"), {"format": "json"})
        data = json.loads(response.content)
        adjustments = data["orange_checks"]

        assert len(adjustments) == 2

        required_fields = [
            "id",
            "team",
            "team_number",
            "description",
            "points_awarded",
            "is_approved",
            "approved_by",
            "approved_at",
            "submitted_by",
            "created_at",
        ]

        for field in required_fields:
            assert field in adjustments[0], f"Missing field: {field}"


class TestInjectScoresExport:
    """Test inject grades export."""

    def test_csv_export_contains_all_required_headers(self, admin_user, inject_grades):
        """CSV export should contain all required headers."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_inject_grades"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.reader(StringIO(content))
        headers = next(reader)

        required_headers = [
            "Team",
            "Team Number",
            "Inject ID",
            "Inject Name",
            "Max Points",
            "Points Awarded",
            "Approved",
            "Approved By",
            "Approved At",
            "Graded By",
            "Graded At",
        ]

        for header in required_headers:
            assert header in headers, f"Missing header: {header}"

    def test_csv_export_data_matches_database(self, admin_user, inject_grades):
        """CSV export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_inject_grades"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)

        assert len(rows) == 2

        # Check first grade
        row1 = rows[0]
        assert row1["Inject ID"] == "INJ-001"
        assert row1["Inject Name"] == "Incident Response Plan"
        assert row1["Max Points"] == "100.00"
        assert row1["Points Awarded"] == "85.00"

    def test_json_export_contains_all_required_fields(self, admin_user, inject_grades):
        """JSON export should contain all required fields."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_inject_grades"), {"format": "json"})
        data = json.loads(response.content)
        grades = data["inject_grades"]

        assert len(grades) == 2

        required_fields = [
            "team",
            "team_number",
            "inject_id",
            "inject_name",
            "max_points",
            "points_awarded",
            "is_approved",
            "approved_by",
            "approved_at",
            "graded_by",
            "graded_at",
        ]

        for field in required_fields:
            assert field in grades[0], f"Missing field: {field}"


class TestFinalScoresExport:
    """Test final scores export."""

    def test_csv_export_contains_all_required_headers(self, admin_user, final_scores):
        """CSV export should contain all required headers."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_final_scores"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.reader(StringIO(content))
        headers = next(reader)

        required_headers = [
            "Rank",
            "Team",
            "Team Number",
            "Total Score",
            "Service Points",
            "Inject Points",
            "Orange Points",
            "Red Deductions",
            "Incident Recovery Points",
            "SLA Penalties",
            "Calculated At",
        ]

        for header in required_headers:
            assert header in headers, f"Missing header: {header}"

    def test_csv_export_data_matches_database(self, admin_user, final_scores):
        """CSV export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_final_scores"), {"format": "csv"})
        content = response.content.decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)

        assert [row["Team"] for row in rows] == ["Team 1", "Team 2", "Team 3"]
        assert [row["Rank"] for row in rows] == ["1", "2", ""]
        assert rows[0]["Total Score"] == "490.00"
        assert rows[0]["Service Points"] == "500.00"
        assert rows[0]["SLA Penalties"] == "-10.00"
        assert rows[0]["Inject Points"] == "0.00"

    def test_json_export_contains_all_required_fields(self, admin_user, final_scores):
        """JSON export should contain all required fields."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_final_scores"), {"format": "json"})
        data = json.loads(response.content)
        scores = data["final_scores"]

        assert len(scores) == 3

        required_fields = [
            "rank",
            "team",
            "team_number",
            "total_score",
            "service_points",
            "inject_points",
            "orange_points",
            "red_deductions",
            "incident_recovery_points",
            "sla_penalties",
            "calculated_at",
        ]

        for field in required_fields:
            assert field in scores[0], f"Missing field: {field}"

    def test_json_export_data_matches_database(self, admin_user, final_scores):
        """JSON export data should match database records."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_final_scores"), {"format": "json"})
        data = json.loads(response.content)
        scores = data["final_scores"]

        # Check first score
        score1 = scores[0]
        assert score1["rank"] == 1
        assert score1["total_score"] == "490.00"
        assert score1["service_points"] == "500.00"


class TestExportFormatParameter:
    """Test format parameter handling across all exports."""

    def test_csv_format_parameter(self, admin_user):
        """?format=csv should return CSV format."""
        client = Client()
        client.force_login(admin_user)

        endpoints = [
            "scoring:export_red_scores",
            "scoring:export_incidents",
            "scoring:export_orange_adjustments",
            "scoring:export_inject_grades",
            "scoring:export_final_scores",
        ]

        for endpoint in endpoints:
            response = client.get(reverse(endpoint), {"format": "csv"})
            assert response["Content-Type"] == "text/csv", f"Failed for {endpoint}"

    def test_json_format_parameter(self, admin_user):
        """?format=json should return JSON format."""
        client = Client()
        client.force_login(admin_user)

        endpoints = [
            "scoring:export_red_scores",
            "scoring:export_incidents",
            "scoring:export_orange_adjustments",
            "scoring:export_inject_grades",
            "scoring:export_final_scores",
        ]

        for endpoint in endpoints:
            response = client.get(reverse(endpoint), {"format": "json"})
            assert response["Content-Type"] == "application/json", f"Failed for {endpoint}"

    def test_default_format(self, admin_user):
        """No format parameter should default to CSV."""
        client = Client()
        client.force_login(admin_user)

        endpoints = [
            "scoring:export_red_scores",
            "scoring:export_incidents",
            "scoring:export_orange_adjustments",
            "scoring:export_inject_grades",
            "scoring:export_final_scores",
        ]

        for endpoint in endpoints:
            response = client.get(reverse(endpoint))
            assert response["Content-Type"] == "text/csv", f"Failed for {endpoint}"

    def test_case_insensitive_format(self, admin_user):
        """Format parameter should be case insensitive."""
        client = Client()
        client.force_login(admin_user)

        # Test CSV
        response = client.get(reverse("scoring:export_red_scores"), {"format": "CSV"})
        assert response["Content-Type"] == "text/csv"

        # Test JSON
        response = client.get(reverse("scoring:export_red_scores"), {"format": "JSON"})
        assert response["Content-Type"] == "application/json"


class TestExportIndexPermissions:
    """Test export index page permission checks."""

    def test_export_index_requires_gold_team(self, create_user_with_groups):
        """Export index page requires Gold Team access."""
        non_gold = create_user_with_groups("red_user", ["WCComps_RedTeam"])
        client = Client()
        client.force_login(non_gold)

        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 302

    def test_export_index_accessible_by_gold_team(self, admin_user):
        """Gold Team can access export index page."""
        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_index"))
        assert response.status_code == 200
        assert b"Export" in response.content


EXPORT_DOWNLOADS = [
    ("export_red_scores", "/scoring/export/red-scores/", "red_findings"),
    ("export_incidents", "/scoring/export/incidents/", "incidents"),
    ("export_orange_adjustments", "/scoring/export/orange-adjustments/", "orange_checks"),
    ("export_inject_grades", "/scoring/export/inject-grades/", "inject_grades"),
    ("export_final_scores", "/scoring/export/final-scores/", "final_scores"),
    ("export_tickets", "/scoring/export/tickets/", "tickets"),
]


class TestExportDownloads:
    """Pin each export's URL, content type and download filename."""

    @pytest.mark.parametrize(("url_name", "path", "filename"), EXPORT_DOWNLOADS)
    @pytest.mark.parametrize(
        ("export_format", "extension", "content_type"),
        [(None, "csv", "text/csv"), ("csv", "csv", "text/csv"), ("json", "json", "application/json")],
    )
    def test_download(self, admin_user, url_name, path, filename, export_format, extension, content_type):
        assert reverse(f"scoring:{url_name}") == path
        client = Client()
        client.force_login(admin_user)

        response = client.get(path, {"format": export_format} if export_format else {})

        assert response.status_code == 200
        assert response["Content-Type"] == content_type
        assert response["Content-Disposition"] == f'attachment; filename="{filename}.{extension}"'

    def test_zip_holds_every_export_in_both_formats(self, admin_user):
        import io
        import zipfile

        client = Client()
        client.force_login(admin_user)

        response = client.get(reverse("scoring:export_all"))

        assert response["Content-Type"] == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
        assert names == [f"{filename}.{ext}" for _, _, filename in EXPORT_DOWNLOADS for ext in ("csv", "json")]
