import json
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO, StringIO

from django.http import HttpResponse
from django.utils import timezone

from core.csv_export import csv_writer

from .calculator import compute_standings
from .models import (
    IncidentReport,
    InjectScore,
    OrangeTeamScore,
    RedTeamScore,
)


def _serialize_red_scores_csv() -> str:
    output = StringIO()
    writer = csv_writer(output)
    writer.writerow(
        [
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
    )
    findings = RedTeamScore.objects.prefetch_related("affected_teams", "approved_by", "submitted_by").order_by(
        "-created_at"
    )
    for finding in findings:
        affected_teams = ", ".join(team.team_name for team in finding.affected_teams.all())
        affected_boxes = ", ".join(finding.affected_boxes) if finding.affected_boxes else ""
        writer.writerow(
            [
                finding.id,
                finding.attack_vector,
                finding.source_ip,
                finding.destination_ip_template,
                affected_boxes,
                finding.affected_service,
                affected_teams,
                finding.points_per_team,
                finding.universally_attempted,
                finding.persistence_established,
                finding.is_approved,
                finding.approved_by.username if finding.approved_by else "",
                finding.approved_at.isoformat() if finding.approved_at else "",
                finding.submitted_by.username if finding.submitted_by else "",
                finding.created_at.isoformat(),
            ]
        )
    return output.getvalue()


def _serialize_red_scores_json() -> str:
    findings = RedTeamScore.objects.prefetch_related("affected_teams", "approved_by", "submitted_by").order_by(
        "-created_at"
    )
    data = []
    for finding in findings:
        affected_teams = [team.team_name for team in finding.affected_teams.all()]
        data.append(
            {
                "id": finding.id,
                "attack_vector": finding.attack_vector,
                "source_ip": finding.source_ip,
                "destination_ip_template": finding.destination_ip_template,
                "affected_boxes": finding.affected_boxes,
                "affected_service": finding.affected_service,
                "affected_teams": affected_teams,
                "points_per_team": str(finding.points_per_team),
                "universally_attempted": finding.universally_attempted,
                "persistence_established": finding.persistence_established,
                "is_approved": finding.is_approved,
                "approved_by": finding.approved_by.username if finding.approved_by else None,
                "approved_at": finding.approved_at.isoformat() if finding.approved_at else None,
                "submitted_by": finding.submitted_by.username if finding.submitted_by else None,
                "created_at": finding.created_at.isoformat(),
            }
        )
    return json.dumps({"red_findings": data}, indent=2)


def _serialize_incidents_csv() -> str:
    output = StringIO()
    writer = csv_writer(output)
    writer.writerow(
        [
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
    )
    incidents = IncidentReport.objects.select_related(
        "team", "submitted_by", "approved_by", "matched_to_red_score"
    ).order_by("-created_at")
    for incident in incidents:
        writer.writerow(
            [
                incident.id,
                incident.team.team_name,
                incident.attack_description,
                incident.source_ip,
                incident.destination_ip or "",
                ", ".join(incident.affected_boxes) if incident.affected_boxes else "",
                incident.affected_service,
                incident.attack_detected_at.isoformat(),
                incident.attack_mitigated,
                incident.points_returned,
                incident.is_approved,
                incident.matched_to_red_score.id if incident.matched_to_red_score else "",
                incident.approved_by.username if incident.approved_by else "",
                incident.approved_at.isoformat() if incident.approved_at else "",
                incident.submitted_by.username if incident.submitted_by else "",
                incident.created_at.isoformat(),
            ]
        )
    return output.getvalue()


def _serialize_incidents_json() -> str:
    incidents = IncidentReport.objects.select_related(
        "team", "submitted_by", "approved_by", "matched_to_red_score"
    ).order_by("-created_at")
    data = [
        {
            "id": incident.id,
            "team": incident.team.team_name,
            "team_number": incident.team.team_number,
            "attack_description": incident.attack_description,
            "source_ip": incident.source_ip,
            "destination_ip": incident.destination_ip,
            "affected_boxes": incident.affected_boxes,
            "affected_service": incident.affected_service,
            "attack_detected_at": incident.attack_detected_at.isoformat(),
            "attack_mitigated": incident.attack_mitigated,
            "points_returned": str(incident.points_returned),
            "is_approved": incident.is_approved,
            "matched_to_red_score_id": (incident.matched_to_red_score.id if incident.matched_to_red_score else None),
            "approved_by": incident.approved_by.username if incident.approved_by else None,
            "approved_at": incident.approved_at.isoformat() if incident.approved_at else None,
            "submitted_by": incident.submitted_by.username if incident.submitted_by else None,
            "created_at": incident.created_at.isoformat(),
        }
        for incident in incidents
    ]
    return json.dumps({"incidents": data}, indent=2)


def _serialize_orange_adjustments_csv() -> str:
    output = StringIO()
    writer = csv_writer(output)
    writer.writerow(
        [
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
    )
    bonuses = OrangeTeamScore.objects.select_related("team", "submitted_by", "approved_by").order_by("-created_at")
    for bonus in bonuses:
        writer.writerow(
            [
                bonus.id,
                bonus.team.team_name,
                bonus.description,
                bonus.points_awarded,
                bonus.is_approved,
                bonus.approved_by.username if bonus.approved_by else "",
                bonus.approved_at.isoformat() if bonus.approved_at else "",
                bonus.submitted_by.username if bonus.submitted_by else "",
                bonus.created_at.isoformat(),
            ]
        )
    return output.getvalue()


def _serialize_orange_adjustments_json() -> str:
    bonuses = OrangeTeamScore.objects.select_related("team", "submitted_by", "approved_by").order_by("-created_at")
    data = [
        {
            "id": bonus.id,
            "team": bonus.team.team_name,
            "team_number": bonus.team.team_number,
            "description": bonus.description,
            "points_awarded": str(bonus.points_awarded),
            "is_approved": bonus.is_approved,
            "approved_by": bonus.approved_by.username if bonus.approved_by else None,
            "approved_at": bonus.approved_at.isoformat() if bonus.approved_at else None,
            "submitted_by": bonus.submitted_by.username if bonus.submitted_by else None,
            "created_at": bonus.created_at.isoformat(),
        }
        for bonus in bonuses
    ]
    return json.dumps({"orange_checks": data}, indent=2)


def _serialize_inject_grades_csv() -> str:
    output = StringIO()
    writer = csv_writer(output)
    writer.writerow(
        [
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
    )
    grades = InjectScore.objects.select_related("team", "graded_by", "approved_by").order_by(
        "inject_name", "team__team_number"
    )
    for grade in grades:
        writer.writerow(
            [
                grade.team.team_name,
                grade.team.team_number,
                grade.inject_id,
                grade.inject_name,
                grade.max_points,
                grade.points_awarded,
                grade.is_approved,
                grade.approved_by.username if grade.approved_by else "",
                grade.approved_at.isoformat() if grade.approved_at else "",
                grade.graded_by.username if grade.graded_by else "",
                grade.graded_at.isoformat(),
            ]
        )
    return output.getvalue()


def _serialize_inject_grades_json() -> str:
    grades = InjectScore.objects.select_related("team", "graded_by", "approved_by").order_by(
        "inject_name", "team__team_number"
    )
    data = [
        {
            "team": grade.team.team_name,
            "team_number": grade.team.team_number,
            "inject_id": grade.inject_id,
            "inject_name": grade.inject_name,
            "max_points": str(grade.max_points),
            "points_awarded": str(grade.points_awarded),
            "is_approved": grade.is_approved,
            "approved_by": grade.approved_by.username if grade.approved_by else None,
            "approved_at": grade.approved_at.isoformat() if grade.approved_at else None,
            "graded_by": grade.graded_by.username if grade.graded_by else None,
            "graded_at": grade.graded_at.isoformat(),
        }
        for grade in grades
    ]
    return json.dumps({"inject_grades": data}, indent=2)


def _serialize_final_scores_csv() -> str:
    output = StringIO()
    writer = csv_writer(output)
    writer.writerow(
        [
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
            "Point Adjustments",
            "Calculated At",
        ]
    )
    scores = compute_standings()
    calculated_at = timezone.now().isoformat()
    for score in scores:
        writer.writerow(
            [
                score.rank or "",
                score.team.team_name,
                score.team.team_number,
                score.total_score,
                score.service_points,
                score.inject_points,
                score.orange_points,
                score.red_deductions,
                score.incident_recovery_points,
                score.sla_penalties,
                score.point_adjustments,
                calculated_at,
            ]
        )
    return output.getvalue()


def _serialize_final_scores_json() -> str:
    scores = compute_standings()
    calculated_at = timezone.now().isoformat()
    data = [
        {
            "rank": score.rank,
            "team": score.team.team_name,
            "team_number": score.team.team_number,
            "total_score": str(score.total_score),
            "service_points": str(score.service_points),
            "inject_points": str(score.inject_points),
            "orange_points": str(score.orange_points),
            "red_deductions": str(score.red_deductions),
            "incident_recovery_points": str(score.incident_recovery_points),
            "sla_penalties": str(score.sla_penalties),
            "point_adjustments": str(score.point_adjustments),
            "calculated_at": calculated_at,
        }
        for score in scores
    ]
    return json.dumps({"final_scores": data}, indent=2)


def _serialize_tickets_csv() -> str:
    from ticketing.models import Ticket

    output = StringIO()
    writer = csv_writer(output)
    writer.writerow(
        [
            "Ticket Number",
            "Team",
            "Team Number",
            "Category",
            "Title",
            "Description",
            "Status",
            "Points Charged",
            "Hostname",
            "IP Address",
            "Service Name",
            "Assigned To",
            "Assigned At",
            "Resolved By",
            "Resolved At",
            "Resolution Notes",
            "Approved",
            "Approved By",
            "Approved At",
            "Created At",
        ]
    )
    tickets = Ticket.objects.select_related("team", "category", "assigned_to", "resolved_by", "approved_by").order_by(
        "-created_at"
    )
    for ticket in tickets:
        writer.writerow(
            [
                ticket.ticket_number,
                ticket.team.team_name,
                ticket.team.team_number,
                ticket.category.display_name if ticket.category else "",
                ticket.title,
                ticket.description,
                ticket.status,
                ticket.points_charged,
                ticket.hostname,
                ticket.ip_address or "",
                ticket.service_name,
                ticket.assigned_to.username if ticket.assigned_to else "",
                ticket.assigned_at.isoformat() if ticket.assigned_at else "",
                ticket.resolved_by.username if ticket.resolved_by else "",
                ticket.resolved_at.isoformat() if ticket.resolved_at else "",
                ticket.resolution_notes,
                ticket.is_approved,
                ticket.approved_by.username if ticket.approved_by else "",
                ticket.approved_at.isoformat() if ticket.approved_at else "",
                ticket.created_at.isoformat(),
            ]
        )
    return output.getvalue()


def _serialize_tickets_json() -> str:
    from ticketing.models import Ticket

    tickets = Ticket.objects.select_related("team", "category", "assigned_to", "resolved_by", "approved_by").order_by(
        "-created_at"
    )
    data = [
        {
            "ticket_number": ticket.ticket_number,
            "team": ticket.team.team_name,
            "team_number": ticket.team.team_number,
            "category": ticket.category.display_name if ticket.category else None,
            "title": ticket.title,
            "description": ticket.description,
            "status": ticket.status,
            "points_charged": ticket.points_charged,
            "hostname": ticket.hostname,
            "ip_address": ticket.ip_address,
            "service_name": ticket.service_name,
            "assigned_to": ticket.assigned_to.username if ticket.assigned_to else None,
            "assigned_at": ticket.assigned_at.isoformat() if ticket.assigned_at else None,
            "resolved_by": ticket.resolved_by.username if ticket.resolved_by else None,
            "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
            "resolution_notes": ticket.resolution_notes,
            "is_approved": ticket.is_approved,
            "approved_by": ticket.approved_by.username if ticket.approved_by else None,
            "approved_at": ticket.approved_at.isoformat() if ticket.approved_at else None,
            "created_at": ticket.created_at.isoformat(),
        }
        for ticket in tickets
    ]
    return json.dumps({"tickets": data}, indent=2)


@dataclass(frozen=True)
class Dataset:
    filename: str
    to_csv: Callable[[], str]
    to_json: Callable[[], str]


DATASETS = {
    "red_scores": Dataset("red_findings", _serialize_red_scores_csv, _serialize_red_scores_json),
    "incidents": Dataset("incidents", _serialize_incidents_csv, _serialize_incidents_json),
    "orange_adjustments": Dataset(
        "orange_checks", _serialize_orange_adjustments_csv, _serialize_orange_adjustments_json
    ),
    "inject_grades": Dataset("inject_grades", _serialize_inject_grades_csv, _serialize_inject_grades_json),
    "final_scores": Dataset("final_scores", _serialize_final_scores_csv, _serialize_final_scores_json),
    "tickets": Dataset("tickets", _serialize_tickets_csv, _serialize_tickets_json),
}


def export_dataset(name: str, export_format: str) -> HttpResponse:
    """One dataset as a JSON download when export_format is "json", CSV otherwise."""
    dataset = DATASETS[name]
    if export_format == "json":
        response = HttpResponse(dataset.to_json(), content_type="application/json")
        extension = "json"
    else:
        response = HttpResponse(dataset.to_csv(), content_type="text/csv")
        extension = "csv"
    response["Content-Disposition"] = f'attachment; filename="{dataset.filename}.{extension}"'
    return response


def export_all_zip_bytes() -> bytes:
    """Every dataset, as CSV and JSON, in one zip."""
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for dataset in DATASETS.values():
            zip_file.writestr(f"{dataset.filename}.csv", dataset.to_csv())
            zip_file.writestr(f"{dataset.filename}.json", dataset.to_json())
    return zip_buffer.getvalue()


def export_all_zip() -> HttpResponse:
    """Export all scoring data as a zip file containing CSV and JSON files."""
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    response = HttpResponse(export_all_zip_bytes(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="wccomps_export_{timestamp}.zip"'
    return response
