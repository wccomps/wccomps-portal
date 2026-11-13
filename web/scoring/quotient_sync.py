import ipaddress
import logging
import re
from decimal import Decimal
from typing import TypedDict

import httpx
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models, transaction
from quotient.client import QuotientClient, ServiceCheck

from team.models import MAX_TEAMS, Team

from .maxes import inject_max_from_titles, service_max_from_export, set_template_max
from .models import QuotientMetadataCache, ScoringTemplate, ServiceDetail, ServiceScore

logger = logging.getLogger(__name__)
_DNS_RECORD_DOMAIN_RE = re.compile(r"^record\s+([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\.?(?=[\s:\->]|$)")
_DNS_ANSWER_IPV4_PREFIX_RE = re.compile(
    r"(?:^record\s+\S+\s*(?::|->\s*(?:acceptable answers were:\s*)?)|(?:within|acceptable answers were:)\s*)"
    r"\[(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b"
)


class ServiceData(TypedDict):
    """Service metadata from Quotient."""

    name: str
    display_name: str


class BoxData(TypedDict):
    """Box metadata from Quotient."""

    name: str
    ip: str
    services: list[ServiceData]


def _sample_team_numbers() -> list[int]:
    return list(
        Team.objects.filter(is_active=True, team_number__gte=1, team_number__lte=MAX_TEAMS)
        .order_by(models.Case(models.When(school_info__isnull=False, then=0), default=1), "team_number")
        .values_list("team_number", flat=True)
    )


def _merge_proxmox_boxes(boxes_data: list[BoxData], previous_boxes: list[BoxData]) -> None:
    """Append team VMs from Proxmox (such as routers/firewalls without scored services) not already in Quotient."""
    from core.proxmox import get_proxmox_client, normalize_ticket_host

    sample_teams = _sample_team_numbers()
    team_number = sample_teams[0] if sample_teams else 1
    known_hosts = {normalize_ticket_host(team_number, box["name"]) or box["name"].lower() for box in boxes_data}
    vms = get_proxmox_client().list_team_vms(team_number)
    if not vms:
        # Proxmox unconfigured, unreachable, or pool empty: keep previously synced unscored boxes
        for cached_box in previous_boxes:
            name = cached_box.get("name", "")
            host = normalize_ticket_host(team_number, name) or name.lower()
            if name and not cached_box.get("services") and not cached_box.get("ip") and host not in known_hosts:
                known_hosts.add(host)
                boxes_data.append({"name": name, "ip": "", "services": []})
        return

    uppercase_names = bool(boxes_data) and all(box["name"].isupper() for box in boxes_data if box["name"])
    for vm in vms:
        if vm.host in known_hosts:
            continue
        known_hosts.add(vm.host)
        box_name = vm.host.upper() if uppercase_names else vm.host
        boxes_data.append({"name": box_name, "ip": "", "services": []})


def _parse_dns_box_ip_template(ip_template: str) -> tuple[str, int] | None:
    octets = ip_template.strip().split(".")
    if len(octets) != 4 or not octets[3].isdigit():
        return None
    last_octet = int(octets[3])
    if not 1 <= last_octet <= 254:
        return None
    prefix = re.sub(r"[_xX]+", "xx", ".".join(octets[:3]) + ".", count=1)
    if prefix.count("xx") != 1 or re.search(r"[_xX]", prefix.replace("xx", "")):
        return None
    try:
        ipaddress.IPv4Address(f"{prefix.replace('xx', '01')}{last_octet}")
    except ValueError:
        return None
    return prefix, last_octet


def _extract_dns_check_network(checks: list[ServiceCheck] | None) -> tuple[str, str]:
    domain = ""
    internal_mask = ""
    for check in checks or ():
        debug = check.debug or ""
        if not domain and (domain_match := _DNS_RECORD_DOMAIN_RE.search(debug)):
            fqdn = domain_match.group(1).lower()
            if not fqdn.endswith(".arpa") and not all(part.isdigit() for part in fqdn.split(".")):
                domain = fqdn.split(".", 1)[1] if fqdn.count(".") >= 2 else fqdn
        if not internal_mask and (ip_match := _DNS_ANSWER_IPV4_PREFIX_RE.search(debug)):
            try:
                ipaddress.IPv4Address(f"{ip_match.group(1)}.1")
                internal_mask = f"{ip_match.group(1)}."
            except ValueError:
                pass
        if domain and internal_mask:
            break
    return domain, internal_mask


def _sync_aweful_proxy(client: QuotientClient, boxes_data: list[BoxData]) -> None:
    proxy_url = settings.AWEFUL_PROXY_URL.strip().rstrip("/")
    if not proxy_url:
        return

    payload: dict[str, int | str] = {}
    sample_teams = _sample_team_numbers()
    if sample_teams:
        payload["number_of_teams"] = max(sample_teams)

    for box in boxes_data:
        dns_service = next(
            (
                s["name"]
                for s in box.get("services", [])
                if s.get("name", "").upper() == "DNS" or s.get("display_name", "").upper() == "DNS"
            ),
            None,
        )
        if not dns_service:
            continue
        if "external_mask" not in payload and (parsed := _parse_dns_box_ip_template(box.get("ip", ""))):
            payload["external_mask"], payload["team_dns_server_last_octet"] = parsed
        for sample_team in sample_teams[:3] or [1]:
            checks = client.get_service_checks(
                sample_team,
                f"{box['name']}:{dns_service}",
                force_refresh=True,
            )
            domain, internal_mask = _extract_dns_check_network(checks)
            if domain and "team_domain_name" not in payload:
                payload["team_domain_name"] = domain
            if internal_mask and "internal_mask" not in payload:
                payload["internal_mask"] = internal_mask
            if "team_domain_name" in payload and "internal_mask" in payload:
                break
        if "external_mask" in payload and "team_domain_name" in payload and "internal_mask" in payload:
            break

    if not payload:
        return

    headers = {"Authorization": f"Bearer {token}"} if (token := settings.AWEFUL_PROXY_TOKEN.strip()) else None
    try:
        response = httpx.post(
            f"{proxy_url}/config",
            json=payload,
            headers=headers,
            timeout=settings.HTTPX_DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning(f"Failed to push config to aweful-proxy at {proxy_url}: {e}")


def sync_quotient_metadata(user: User | None = None) -> QuotientMetadataCache:
    """Sync the box, service and IP metadata behind the scoring form dropdowns from Quotient and Proxmox.

    Raises ValueError if Quotient is unreachable. The last synced metadata stays: one failed fetch mid-competition
    must not empty the red-team and incident dropdowns. Competition cleanup deletes it between events.
    """
    client = QuotientClient()
    # A sync is an explicit fetch; the client's 5-minute cache is for page renders
    infrastructure = client.get_infrastructure(force_refresh=True)

    if not infrastructure:
        raise ValueError("Failed to retrieve infrastructure from Quotient")

    boxes_data: list[BoxData] = []
    services_data: list[ServiceData] = []

    for box in infrastructure.boxes:
        box_services: list[ServiceData] = []

        for service in box.services:
            service_dict: ServiceData = {
                "name": service.name,
                "display_name": service.display_name,
            }
            box_services.append(service_dict)

            if service_dict not in services_data:
                services_data.append(service_dict)

        box_dict: BoxData = {
            "name": box.name,
            "ip": box.ip,
            "services": box_services,
        }
        boxes_data.append(box_dict)

    metadata = QuotientMetadataCache.objects.first()
    _merge_proxmox_boxes(boxes_data, metadata.boxes if metadata else [])
    if metadata:
        metadata.boxes = boxes_data
        metadata.services = services_data
        metadata.synced_by = user
        metadata.save()
    else:
        metadata = QuotientMetadataCache.objects.create(
            boxes=boxes_data,
            services=services_data,
            synced_by=user,
        )

    # Keep the reactive inject scaling current: inject point values live in the titles.
    injects = client.get_injects(force_refresh=True)
    if injects:
        set_template_max("inject_max", inject_max_from_titles(injects))

    _sync_aweful_proxy(client, boxes_data)

    return metadata


def _check_storable(row: models.Model, where: str) -> None:
    """ValueError when a value Quotient sent doesn't fit its column (a name too long, points too large)."""
    try:
        row.clean_fields(exclude=["team", "synced_by"])
    except ValidationError as e:
        raise ValueError(f"Quotient's scores for {where} don't fit the portal's: {'; '.join(e.messages)}") from e


class ServiceSyncResult(TypedDict):
    """How many teams a service score sync wrote, and what to tell whoever ran it when the service maximum was left
    as it was (None when it was stored, or the export had no checks to add up)."""

    total: int
    max_not_updated: str | None


def sync_service_scores(user: User | None = None) -> ServiceSyncResult:
    """Sync each team's aggregate ServiceScore and per-service ServiceDetail records from Quotient, removing and
    skipping teams with no services in the export, and store the service maximum the export gives. Raises
    ValueError, writing nothing, when Quotient's scores can't be read or don't fit the portal's columns."""
    client = QuotientClient()

    export_data = client.get_service_export()
    if export_data is None:
        raise ValueError("Failed to retrieve service scores from Quotient")

    total = 0

    rows: list[tuple[Team, ServiceScore, list[ServiceDetail]]] = []
    for team_export in export_data:
        team = Team.objects.filter(team_number=team_export.team_number).first() if team_export.team_number else None
        if team is None:
            continue
        score = ServiceScore(
            team=team,
            service_points=Decimal(team_export.gross_points),
            sla_violations=Decimal(-team_export.total_sla_penalty),
            synced_by=user,
        )
        details = [
            ServiceDetail(
                team=team,
                service_name=svc.service_name,
                points=Decimal(svc.service_points),
                sla_violations=svc.sla_violations,
            )
            for svc in team_export.services
        ]
        for row in (score, *details):
            _check_storable(row, f"team {team.team_number}")
        rows.append((team, score, details))
    service_max = service_max_from_export(export_data)
    if service_max:
        _check_storable(ScoringTemplate(service_max=service_max), "the service maximum")

    with transaction.atomic():
        for team, score, details in rows:
            ServiceDetail.objects.filter(team=team).delete()
            if not details:
                ServiceScore.objects.filter(team=team).delete()
                continue
            ServiceScore.objects.update_or_create(
                team=team,
                defaults={
                    "service_points": score.service_points,
                    "sla_violations": score.sla_violations,
                    "synced_by": user,
                },
            )
            ServiceDetail.objects.bulk_create(details)
            total += 1

    max_not_updated = None
    if service_max is None:
        max_not_updated = (
            "The service maximum was not updated: Quotient's export gave no max points, so scores still scale by "
            "the maximum already stored."
        )
    elif service_max and not set_template_max("service_max", service_max):
        max_not_updated = (
            "The service maximum was not stored: no scoring setup is saved yet. Open Scoring setup, then sync again."
        )
    return {"total": total, "max_not_updated": max_not_updated}


def get_box_metadata() -> dict[str, dict[str, object]]:
    """Each box's IP and service names from cached metadata, for the forms' IP auto-fill and service filter."""
    metadata = QuotientMetadataCache.objects.first()
    if not metadata:
        return {}
    return {
        box["name"]: {"ip": box["ip"], "services": [svc["name"] for svc in box.get("services", [])]}
        for box in metadata.boxes
    }


def ticket_infrastructure(*, staff: bool = False) -> tuple[list[str], dict[str, str], list[dict[str, str]]]:
    """Box names, box IPs and service choices (keys: value, label, box_ip, box_name, service_name) for filing a
    ticket, from cached metadata. Services are omitted for non-staff while applications are disabled."""
    from core.models import CompetitionConfig

    metadata = QuotientMetadataCache.objects.first()
    boxes = metadata.boxes if metadata else []
    show_services = staff or CompetitionConfig.get_config().applications_enabled
    services = (
        [
            {
                "value": f"{box['name']}:{svc['name']}",
                "label": f"{box['name']}: {svc['display_name']}",
                "box_ip": box["ip"],
                "box_name": box["name"],
                "service_name": svc["name"],
            }
            for box in boxes
            for svc in box.get("services", [])
        ]
        if show_services
        else []
    )
    return (
        sorted(box["name"] for box in boxes),
        {box["name"]: box["ip"] for box in boxes},
        sorted(services, key=lambda s: s["label"]),
    )


def get_box_choices() -> list[tuple[str, str]]:
    """Box dropdown choices from cached metadata; labels lead with the last IP octet to tell boxes apart."""
    metadata = QuotientMetadataCache.objects.first()
    if metadata:
        choices = []
        for box in metadata.boxes:
            ip = box.get("ip", "")
            last_octet = ip.split(".")[-1] if ip else ""
            label = f".{last_octet} {box['name']}" if last_octet else box["name"]
            choices.append((box["name"], label))
        return choices
    return []


def get_service_choices() -> list[tuple[str, str]]:
    """Service dropdown choices from cached metadata, ending with 'Other' for unscored services."""
    metadata = QuotientMetadataCache.objects.first()
    raw = [*(s for b in metadata.boxes for s in b.get("services", [])), *metadata.services] if metadata else []
    by_name = {s["name"]: s.get("display_name") or s["name"] for s in raw if s.get("name") and s.get("name") != "Other"}
    return [*by_name.items(), ("Other", "Other")]
