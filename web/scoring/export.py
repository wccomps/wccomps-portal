import json
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO, StringIO
from typing import Protocol, TypedDict

from django.conf import settings
from django.db.models import BinaryField, CharField, F, Func, Value
from django.db.models.functions import Length
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from django_stubs_ext import WithAnnotations

from core.csv_export import csv_writer
from ticketing.chat_comments import comment_origin
from ticketing.models import Ticket, TicketAttachment, TicketComment, TicketHistory

from .calculator import Standing, compute_standings
from .models import (
    IncidentReport,
    InjectScore,
    OrangeTeamScore,
    RedTeamScore,
)


def _csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    return value


@dataclass(frozen=True)
class Column[T]:
    csv_header: str | None
    json_key: str
    extract: Callable[[T], object]


class ExportableDataset(Protocol):
    @property
    def filename(self) -> str: ...
    def render_pair(self) -> tuple[str, str]: ...
    def to_csv(self) -> str: ...
    def to_json(self) -> str: ...


@dataclass(frozen=True)
class Dataset[T]:
    filename: str
    columns: Sequence[Column[T]]
    fetch: Callable[[], Sequence[T]]

    def render_csv(self, rows: Sequence[T]) -> str:
        output = StringIO()
        writer = csv_writer(output)
        csv_cols = [col for col in self.columns if col.csv_header is not None]
        writer.writerow([col.csv_header for col in csv_cols])
        for row in rows:
            writer.writerow([_csv_cell(col.extract(row)) for col in csv_cols])
        return output.getvalue()

    def render_json(self, rows: Sequence[T]) -> str:
        data = [{col.json_key: _json_value(col.extract(row)) for col in self.columns} for row in rows]
        return json.dumps({self.filename: data}, indent=2)

    def render_pair(self) -> tuple[str, str]:
        rows = self.fetch()
        return self.render_csv(rows), self.render_json(rows)

    def to_csv(self) -> str:
        return self.render_csv(self.fetch())

    def to_json(self) -> str:
        return self.render_json(self.fetch())


def _fetch_red_scores() -> list[RedTeamScore]:
    return list(
        RedTeamScore.objects.select_related("attack_type", "approved_by", "submitted_by")
        .prefetch_related("affected_teams")
        .order_by("-created_at")
    )


def _fetch_incidents() -> list[IncidentReport]:
    return list(
        IncidentReport.objects.select_related("team", "submitted_by", "approved_by", "matched_to_red_score").order_by(
            "-created_at"
        )
    )


def _fetch_orange_adjustments() -> list[OrangeTeamScore]:
    return list(
        OrangeTeamScore.objects.select_related("team", "submitted_by", "approved_by", "orange_check").order_by(
            "-created_at"
        )
    )


def _fetch_inject_grades() -> list[InjectScore]:
    return list(
        InjectScore.objects.select_related("team", "graded_by", "approved_by").order_by(
            "inject_name", "team__team_number"
        )
    )


def _fetch_final_scores() -> list[tuple[Standing, str]]:
    calculated_at = timezone.now().isoformat()
    return [(score, calculated_at) for score in compute_standings()]


def _fetch_tickets() -> list[Ticket]:
    return list(
        Ticket.objects.select_related("team", "category", "assigned_to", "resolved_by", "approved_by").order_by(
            "-created_at"
        )
    )


def _fetch_ticket_comments() -> list[TicketComment]:
    return list(TicketComment.objects.select_related("ticket__team", "author").order_by("ticket__ticket_number", "pk"))


def _comment_source(comment: TicketComment) -> str:
    """Where the comment was written: the chat backend's name, or "web" for the portal's ticket page."""
    origin = comment_origin(comment)
    return origin.value if origin else "web"


def _fetch_ticket_history() -> list[TicketHistory]:
    return list(TicketHistory.objects.select_related("ticket__team", "actor").order_by("ticket__ticket_number", "pk"))


class AttachmentDigest(TypedDict):
    size: int
    sha256: str


type DigestedAttachment = WithAnnotations[TicketAttachment, AttachmentDigest]


def _fetch_ticket_attachments() -> list[DigestedAttachment]:
    """Each attachment's record without the file (teams may store 200MB each; the manager never reads it), with the
    file's size and SHA-256 worked out in Postgres, so a copy found elsewhere can be matched to it."""
    sha256 = Func(
        Func(F("file_data"), function="SHA256", output_field=BinaryField()),
        Value("hex"),
        function="ENCODE",
        output_field=CharField(),
    )
    return list(
        TicketAttachment.objects.select_related("ticket__team")
        .order_by("ticket__ticket_number", "pk")
        .annotate(size=Length("file_data"), sha256=sha256)
    )


DATASETS: dict[str, ExportableDataset] = {
    "red_scores": Dataset[RedTeamScore](
        "red_findings",
        [
            Column("ID", "id", lambda f: f.id),
            Column("Attack Type", "attack_type", lambda f: f.attack_type.name if f.attack_type else None),
            Column("Source IP", "source_ip", lambda f: f.source_ip),
            Column("Destination IP Template", "destination_ip_template", lambda f: f.destination_ip_template),
            Column("Affected Boxes", "affected_boxes", lambda f: f.affected_boxes),
            Column("Affected Service", "affected_service", lambda f: f.affected_service),
            Column("Affected Teams", "affected_teams", lambda f: [t.team_name for t in f.affected_teams.all()]),
            Column("Points Per Team", "points_per_team", lambda f: f.points_per_team),
            Column("Universally Attempted", "universally_attempted", lambda f: f.universally_attempted),
            Column("Persistence Established", "persistence_established", lambda f: f.persistence_established),
            Column("Approved", "is_approved", lambda f: f.is_approved),
            Column("Approved By", "approved_by", lambda f: f.approved_by.username if f.approved_by else None),
            Column("Approved At", "approved_at", lambda f: f.approved_at.isoformat() if f.approved_at else None),
            Column("Submitted By", "submitted_by", lambda f: f.submitted_by.username if f.submitted_by else None),
            Column("Created At", "created_at", lambda f: f.created_at.isoformat()),
        ],
        _fetch_red_scores,
    ),
    "incidents": Dataset[IncidentReport](
        "incidents",
        [
            Column("ID", "id", lambda i: i.id),
            Column("Team", "team", lambda i: i.team.team_name),
            Column(None, "team_number", lambda i: i.team.team_number),
            Column("Attack Description", "attack_description", lambda i: i.attack_description),
            Column("Source IP", "source_ip", lambda i: i.source_ip),
            Column("Destination IP", "destination_ip", lambda i: i.destination_ip),
            Column("Affected Boxes", "affected_boxes", lambda i: i.affected_boxes),
            Column("Affected Service", "affected_service", lambda i: i.affected_service),
            Column("Attack Detected At", "attack_detected_at", lambda i: i.attack_detected_at.isoformat()),
            Column("Attack Mitigated", "attack_mitigated", lambda i: i.attack_mitigated),
            Column("Points Returned", "points_returned", lambda i: i.points_returned),
            Column("Reviewed", "is_approved", lambda i: i.is_approved),
            Column(
                "Matched Finding ID",
                "matched_to_red_score_id",
                lambda i: i.matched_to_red_score.id if i.matched_to_red_score else None,
            ),
            Column("Reviewed By", "approved_by", lambda i: i.approved_by.username if i.approved_by else None),
            Column("Reviewed At", "approved_at", lambda i: i.approved_at.isoformat() if i.approved_at else None),
            Column("Submitted By", "submitted_by", lambda i: i.submitted_by.username if i.submitted_by else None),
            Column("Created At", "created_at", lambda i: i.created_at.isoformat()),
        ],
        _fetch_incidents,
    ),
    "orange_adjustments": Dataset[OrangeTeamScore](
        "orange_checks",
        [
            Column("ID", "id", lambda b: b.id),
            Column("Team", "team", lambda b: b.team.team_name),
            Column(None, "team_number", lambda b: b.team.team_number),
            Column("Description", "description", lambda b: b.description),
            Column("Max Points", "max_points", lambda b: b.max_points),
            Column("Points Awarded", "points_awarded", lambda b: b.points_awarded),
            Column("Approved", "is_approved", lambda b: b.is_approved),
            Column("Approved By", "approved_by", lambda b: b.approved_by.username if b.approved_by else None),
            Column("Approved At", "approved_at", lambda b: b.approved_at.isoformat() if b.approved_at else None),
            Column("Submitted By", "submitted_by", lambda b: b.submitted_by.username if b.submitted_by else None),
            Column("Created At", "created_at", lambda b: b.created_at.isoformat()),
        ],
        _fetch_orange_adjustments,
    ),
    "inject_grades": Dataset[InjectScore](
        "inject_grades",
        [
            Column("Team", "team", lambda g: g.team.team_name),
            Column("Team Number", "team_number", lambda g: g.team.team_number),
            Column("Inject ID", "inject_id", lambda g: g.inject_id),
            Column("Inject Name", "inject_name", lambda g: g.inject_name),
            Column("Max Points", "max_points", lambda g: g.max_points),
            Column("Points Awarded", "points_awarded", lambda g: g.points_awarded),
            Column("Approved", "is_approved", lambda g: g.is_approved),
            Column("Approved By", "approved_by", lambda g: g.approved_by.username if g.approved_by else None),
            Column("Approved At", "approved_at", lambda g: g.approved_at.isoformat() if g.approved_at else None),
            Column("Graded By", "graded_by", lambda g: g.graded_by.username if g.graded_by else None),
            Column("Graded At", "graded_at", lambda g: g.graded_at.isoformat()),
        ],
        _fetch_inject_grades,
    ),
    "final_scores": Dataset[tuple[Standing, str]](
        "final_scores",
        [
            Column("Rank", "rank", lambda sc: sc[0].rank),
            Column("Team", "team", lambda sc: sc[0].team.team_name),
            Column("Team Number", "team_number", lambda sc: sc[0].team.team_number),
            Column("Total Score", "total_score", lambda sc: sc[0].total_score),
            Column("Service Points", "service_points", lambda sc: sc[0].service_points),
            Column("Inject Points", "inject_points", lambda sc: sc[0].inject_points),
            Column("Orange Points", "orange_points", lambda sc: sc[0].orange_points),
            Column("Red Deductions", "red_deductions", lambda sc: sc[0].red_deductions),
            Column("Incident Recovery Points", "incident_recovery_points", lambda sc: sc[0].incident_recovery_points),
            Column("SLA Penalties", "sla_penalties", lambda sc: sc[0].sla_penalties),
            Column(
                "Raw Service Credit",
                "raw_service_credit",
                lambda sc: sum(line["service_credit"] for line in sc[0].service_corrections),
            ),
            Column(
                "SLA Credit",
                "sla_credit",
                lambda sc: sum(line["sla_credit"] for line in sc[0].service_corrections),
            ),
            Column("Point Adjustments", "point_adjustments", lambda sc: sc[0].point_adjustments),
            Column("Calculated At", "calculated_at", lambda sc: sc[1]),
        ],
        _fetch_final_scores,
    ),
    "tickets": Dataset[Ticket](
        "tickets",
        [
            Column("Ticket Number", "ticket_number", lambda t: t.ticket_number),
            Column("Team", "team", lambda t: t.team.team_name),
            Column("Team Number", "team_number", lambda t: t.team.team_number),
            Column("Category", "category", lambda t: t.category.display_name if t.category else None),
            Column("Title", "title", lambda t: t.title),
            Column("Description", "description", lambda t: t.description),
            Column("Status", "status", lambda t: t.status),
            Column("Points Charged", "points_charged", lambda t: t.points_charged),
            Column("Hostname", "hostname", lambda t: t.hostname),
            Column("IP Address", "ip_address", lambda t: t.ip_address),
            Column("Service Name", "service_name", lambda t: t.service_name),
            Column("Assigned To", "assigned_to", lambda t: t.assigned_to.username if t.assigned_to else None),
            Column("Assigned At", "assigned_at", lambda t: t.assigned_at.isoformat() if t.assigned_at else None),
            Column("Resolved By", "resolved_by", lambda t: t.resolved_by.username if t.resolved_by else None),
            Column("Resolved At", "resolved_at", lambda t: t.resolved_at.isoformat() if t.resolved_at else None),
            Column("Resolution Notes", "resolution_notes", lambda t: t.resolution_notes),
            Column("Approved", "is_approved", lambda t: t.is_approved),
            Column("Approved By", "approved_by", lambda t: t.approved_by.username if t.approved_by else None),
            Column("Approved At", "approved_at", lambda t: t.approved_at.isoformat() if t.approved_at else None),
            Column("Created At", "created_at", lambda t: t.created_at.isoformat()),
        ],
        _fetch_tickets,
    ),
    "ticket_comments": Dataset[TicketComment](
        "ticket_comments",
        [
            Column("Ticket Number", "ticket_number", lambda c: c.ticket.ticket_number),
            Column("Team Number", "team_number", lambda c: c.ticket.team.team_number),
            Column("Author", "author", lambda c: c.author_name),
            Column("Source", "source", _comment_source),
            Column("Discord Message ID", "discord_message_id", lambda c: c.discord_message_id),
            Column("Zulip Message ID", "zulip_message_id", lambda c: c.zulip_message_id),
            Column("Comment", "comment_text", lambda c: c.comment_text),
            Column("Posted At", "posted_at", lambda c: c.posted_at.isoformat()),
        ],
        _fetch_ticket_comments,
    ),
    "ticket_history": Dataset[TicketHistory](
        "ticket_history",
        [
            Column("Ticket Number", "ticket_number", lambda h: h.ticket.ticket_number),
            Column("Team Number", "team_number", lambda h: h.ticket.team.team_number),
            Column("Action", "action", lambda h: h.action),
            Column("Actor", "actor", lambda h: h.actor.username if h.actor else None),
            Column("Details", "details", lambda h: h.details),
            Column("Timestamp", "timestamp", lambda h: h.timestamp.isoformat()),
        ],
        _fetch_ticket_history,
    ),
    "ticket_attachments": Dataset[DigestedAttachment](
        "ticket_attachments",
        [
            Column("Ticket Number", "ticket_number", lambda a: a.ticket.ticket_number),
            Column("Team Number", "team_number", lambda a: a.ticket.team.team_number),
            Column("Filename", "filename", lambda a: a.filename),
            Column("Type", "mime_type", lambda a: a.mime_type),
            Column("Size", "size", lambda a: a.size),
            Column("SHA-256", "sha256", lambda a: a.sha256),
            Column("Uploaded By", "uploaded_by", lambda a: a.uploaded_by),
            Column("Uploaded At", "uploaded_at", lambda a: a.uploaded_at.isoformat()),
        ],
        _fetch_ticket_attachments,
    ),
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
            csv_str, json_str = dataset.render_pair()
            zip_file.writestr(f"{dataset.filename}.csv", csv_str)
            zip_file.writestr(f"{dataset.filename}.json", json_str)
    return zip_buffer.getvalue()


def export_all_zip() -> HttpResponse:
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    prefix = slugify(settings.ORG_SHORT_NAME)
    response = HttpResponse(export_all_zip_bytes(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{prefix}_export_{timestamp}.zip"'
    return response
