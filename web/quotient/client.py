"""Client for Quotient scoring engine REST API."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import cast
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_INJECT_POINTS_RE = re.compile(r"\(\s*([\d,]+)\s*(?:pts?|points?)\s*\)", re.IGNORECASE)
MAX_CHECK_OUTPUT_CHARS = 16_384
_UNSAFE_OUTPUT_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def inject_points(title: str) -> int | None:
    """The point value embedded in a Quotient inject title, or None if it has none."""
    match = _INJECT_POINTS_RE.search(title or "")
    return int(match.group(1).replace(",", "")) if match else None


def _is_unavailable(exc: BaseException | None) -> bool:
    """True if Quotient is down or unreachable (expected between competitions), not misconfigured."""
    while exc is not None:
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
            return True
        exc = exc.__cause__
    return False


def _log_failure(message: str, exc: Exception) -> None:
    """Log a request failure: one line if Quotient is just down, full traceback otherwise."""
    if not _is_unavailable(exc):
        logger.exception(f"{message}: {exc}")
    elif isinstance(exc, QuotientAPIError):
        # Login already logged the unavailability; don't repeat it for every request
        logger.debug(f"{message}: {exc}")
    else:
        logger.warning(f"{message}: Quotient unavailable ({str(exc).splitlines()[0]})")


def _json(response: httpx.Response) -> object:
    """The answer's JSON; ValueError when it isn't JSON (or is nested too deep to read)."""
    try:
        return response.json()
    except RecursionError as e:
        raise ValueError("the answer is nested too deep to read") from e


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} isn't text: {value!r:.80}")
    return value


def _clean_output(value: object, what: str) -> str:
    """Sanitize untrusted check output from a team's box: strip control/BiDi chars and cap length."""
    cleaned = _UNSAFE_OUTPUT_CHARS_RE.sub("", _text(value, what))
    if len(cleaned) > MAX_CHECK_OUTPUT_CHARS:
        return f"{cleaned[:MAX_CHECK_OUTPUT_CHARS]}\n... [truncated]"
    return cleaned


def _whole(value: object, what: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{what} isn't a whole number: {value!r:.80}")
    return value


def _boolean(value: object, what: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{what} isn't a boolean: {value!r:.80}")
    return value


def _timestamp(value: object, what: str) -> datetime | None:
    """Aware datetime from a Quotient ISO timestamp, or None when blank, unparseable, or Go's 0001-01-01 zero time."""
    raw = _text(value, what)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if not 1 < dt.year < 9999:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _fraction(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError(f"{what} isn't a fraction from 0 to 1: {value!r:.80}")
    return float(value)


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{what} isn't an object: {value!r:.80}")
    return value


def _objects(value: object, what: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{what} isn't a list of objects: {value!r:.80}")
    return value


def _field(data: object, key: str, what: str) -> object:
    obj = _object(data, what)
    if key not in obj:
        raise ValueError(f"{what} has no {key!r}")
    return obj[key]


def _team_number(team_name: str) -> int:
    """Portal team number from a Quotient team name ("team09" -> 9); 0 if the name has no digits."""
    return int("".join(c for c in team_name if c.isdigit()) or "0")


def quotient_service_name(service_name: str, hostname: str = "") -> str:
    """Quotient's check name ("box-service") from a ticket's service_name ("box:service", "box-service", or
    "service" with hostname="box"), or "" if it isn't a valid Quotient check identifier."""
    name = service_name.strip()
    host = hostname.strip()
    if ":" in name:
        box, _, svc = name.partition(":")
        host = box.strip() or host
        name = svc.strip()
    candidate = f"{host}-{name}" if host and name and not name.lower().startswith(f"{host.lower()}-") else name
    if not _SERVICE_NAME_RE.match(candidate) or ".." in candidate:
        return ""
    return candidate


@dataclass
class QuotientService:
    """Represents a service on a box."""

    name: str
    display_name: str


@dataclass
class QuotientBox:
    name: str
    ip: str
    services: list[QuotientService]


@dataclass
class QuotientInfrastructure:
    boxes: list[QuotientBox]


@dataclass
class ServiceExportEntry:
    """Per-service score data from export endpoint."""

    service_name: str
    service_points: int
    sla_violations: int
    sla_penalty: int


@dataclass
class TeamServiceExport:
    team_name: str
    team_number: int
    services: list[ServiceExportEntry]
    gross_points: int
    total_sla_penalty: int
    total_points: int


@dataclass
class TeamUptime:
    team_name: str
    team_number: int
    uptimes: dict[str, float]  # service_name -> uptime (0.0-1.0)


@dataclass
class Inject:
    inject_id: int
    title: str
    description: str
    max_points: int | None = None

    def __post_init__(self) -> None:
        if self.max_points is None:
            self.max_points = inject_points(self.title)


@dataclass
class ServiceCheck:
    round_number: int
    timestamp: datetime | None
    passed: bool
    debug: str
    error: str


class QuotientAPIError(Exception):
    """Raised when Quotient API returns an error."""


class QuotientClient:
    def __init__(
        self,
        base_url: str | None = None,
        cache_ttl: int = 300,
    ):
        base = base_url or str(getattr(settings, "QUOTIENT_API_URL", ""))
        self.base_url = base.rstrip("/")
        self.cache_ttl = cache_ttl
        self.client: httpx.Client | None = None

    def _get_client(self, force_reauth: bool = False) -> httpx.Client:
        if self.client is None or force_reauth:
            self.client = httpx.Client()

            username = getattr(settings, "QUOTIENT_USERNAME", "")
            password = getattr(settings, "QUOTIENT_PASSWORD", "")

            if not username or not password:
                logger.error("QUOTIENT_USERNAME or QUOTIENT_PASSWORD not configured")
                raise QuotientAPIError("Quotient credentials not configured")

            try:
                response = self.client.post(
                    f"{self.base_url}/api/login",
                    json={
                        "username": username,
                        "password": password,
                    },
                    timeout=settings.HTTPX_DEFAULT_TIMEOUT,
                )
                response.raise_for_status()
                logger.info(f"Authenticated with Quotient as {username}")
                return self.client
            except httpx.HTTPError as e:
                if _is_unavailable(e):
                    logger.warning(f"Quotient unavailable: login failed ({str(e).splitlines()[0]})")
                else:
                    logger.exception(f"Failed to authenticate with Quotient: {e}")
                raise QuotientAPIError(f"Authentication failed: {e}") from e

        return self.client

    def _request(self, method: str, endpoint: str, **kwargs: object) -> httpx.Response:
        """Make an authenticated request, re-authenticating on 401."""
        client = self._get_client()
        url = f"{self.base_url}{endpoint}"
        if "timeout" not in kwargs:
            kwargs["timeout"] = 10

        request_func = getattr(client, method)
        response = cast(httpx.Response, request_func(url, **kwargs))

        if response.status_code == 401:
            logger.info("Got 401, re-authenticating with Quotient")
            client = self._get_client(force_reauth=True)
            request_func = getattr(client, method)
            response = cast(httpx.Response, request_func(url, **kwargs))

        return response

    def _cached[T](
        self, cache_key: str, ttl: int, force_refresh: bool, endpoint: str, parse: Callable[[object], T], what: str
    ) -> T | None:
        """What parse makes of the endpoint's answer, from the cache unless asked to refresh; None if unavailable.

        A cached empty answer is a hit: None is never cached, so only a missing key fetches.
        """
        if not force_refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                return cast(T, cached)
        try:
            response = self._request("get", endpoint)
            response.raise_for_status()
            value = parse(_json(response))
        except (httpx.HTTPError, QuotientAPIError) as e:
            _log_failure(f"Failed to fetch {what} from Quotient", e)
            return None
        except ValueError as e:
            logger.exception(f"Failed to parse {what} from Quotient: {e}")
            return None
        cache.set(cache_key, value, ttl)
        return value

    def get_infrastructure(self, force_refresh: bool = False) -> QuotientInfrastructure | None:
        """Fetch infrastructure from Quotient API, or None if unavailable."""
        return self._cached(
            "quotient_infrastructure", self.cache_ttl, force_refresh, "/api/metadata", _infrastructure, "infrastructure"
        )

    def get_injects(self, force_refresh: bool = False) -> list[Inject] | None:
        """Fetch injects from Quotient API, or None if unavailable."""
        return self._cached("quotient_injects", 60, force_refresh, "/api/injects", _injects, "injects")

    def get_service_export(self, force_refresh: bool = False) -> list[TeamServiceExport] | None:
        """Fetch per-service score breakdown from Quotient, or None if unavailable."""
        return self._cached(
            "quotient_service_export",
            self.cache_ttl,
            force_refresh,
            "/api/engine/export/scores",
            _service_export,
            "the service export",
        )

    def get_uptimes(self, force_refresh: bool = False) -> list[TeamUptime] | None:
        """Fetch per-service uptime percentages from Quotient, or None if unavailable."""
        return self._cached(
            "quotient_uptimes", self.cache_ttl, force_refresh, "/api/graphs/uptimes", _uptimes, "uptimes"
        )

    def get_service_choices(self) -> list[dict[str, str]]:
        """Service choices for the ticket dropdown (keys: value, label, box_ip, box_name, service_name)."""
        infrastructure = self.get_infrastructure()
        if not infrastructure:
            return []

        choices = [
            {
                "value": f"{box.name}:{service.name}",
                "label": f"{box.name}: {service.display_name}",
                "box_ip": box.ip,
                "box_name": box.name,
                "service_name": service.name,
            }
            for box in infrastructure.boxes
            for service in box.services
        ]

        return sorted(choices, key=lambda x: x["label"])

    def get_box_names(self) -> list[str]:
        infrastructure = self.get_infrastructure()
        if not infrastructure:
            return []

        return sorted([box.name for box in infrastructure.boxes])

    def get_service_checks(
        self,
        team_number: int,
        service_name: str,
        *,
        force_refresh: bool = False,
    ) -> list[ServiceCheck] | None:
        """Fetch per-round check results for a team's service from Quotient (newest first), or None if unavailable."""
        target = quotient_service_name(service_name)
        if not target or team_number <= 0:
            return []

        return self._cached(
            f"quotient_service_checks_{team_number}_{target}",
            30,
            force_refresh,
            f"/api/services/{team_number}/{quote(target, safe='')}",
            _service_checks,
            "service checks",
        )


def _infrastructure(data: object) -> QuotientInfrastructure:
    # /api/metadata returns: {"boxes": [{"name": str, "ip": str, "services": [str]}]}
    boxes = []
    for box_data in _objects(_object(data, "the metadata").get("boxes", []), "boxes"):
        svc_names = box_data.get("services", [])
        if not isinstance(svc_names, list):
            raise ValueError(f"a box's services aren't a list: {svc_names!r:.80}")
        services = [
            QuotientService(name=_text(svc_name, "a service"), display_name=_text(svc_name, "a service"))
            for svc_name in svc_names
        ]
        boxes.append(
            QuotientBox(
                name=_text(_field(box_data, "name", "a box"), "a box's name"),
                ip=_text(_field(box_data, "ip", "a box"), "a box's IP"),
                services=services,
            )
        )
    logger.info(f"Fetched {len(boxes)} boxes from Quotient API")
    return QuotientInfrastructure(boxes=boxes)


def _injects(data: object) -> list[Inject]:
    inject_list = data if isinstance(data, list) else _field(data, "injects", "the injects")
    injects = [
        Inject(
            inject_id=_whole(_field(i, "ID", "an inject"), "an inject's ID"),
            title=_text(_field(i, "Title", "an inject"), "an inject's title"),
            description=_text(_field(i, "Description", "an inject"), "an inject's description"),
        )
        for i in _objects(inject_list, "injects")
    ]
    logger.info(f"Fetched {len(injects)} injects")
    return injects


def _service_export(data: object) -> list[TeamServiceExport]:
    exports = []
    for team_data in _objects(data, "the export"):
        services = [
            ServiceExportEntry(
                service_name=_text(_field(s, "service_name", "a service"), "a service's name"),
                service_points=_whole(_field(s, "service_points", "a service"), "a service's points"),
                sla_violations=_whole(s.get("sla_violations", 0), "a service's SLA violations"),
                sla_penalty=_whole(s.get("sla_penalty", 0), "a service's SLA penalty"),
            )
            for s in _objects(team_data.get("services", []), "a team's services")
        ]
        team_name = _text(_field(team_data, "team_name", "a team"), "a team's name")
        exports.append(
            TeamServiceExport(
                team_name=team_name,
                team_number=_team_number(team_name),
                services=services,
                gross_points=_whole(team_data.get("gross_points", 0), "a team's points"),
                total_sla_penalty=_whole(team_data.get("total_sla_penalty", 0), "a team's SLA penalty"),
                total_points=_whole(team_data.get("total_points", 0), "a team's total"),
            )
        )
    logger.info(f"Fetched service export for {len(exports)} teams")
    return exports


def _uptimes(data: object) -> list[TeamUptime]:
    result = []
    for team_data in _objects(_object(data, "the uptimes").get("series", []), "the uptimes"):
        team_name = _text(_field(team_data, "Name", "a team"), "a team's name")
        uptimes = {
            _text(_field(entry, "Service", "an uptime"), "a service"): _fraction(
                _field(entry, "Uptime", "an uptime"), "an uptime"
            )
            for entry in _objects(team_data.get("Data", []), "a team's uptimes")
        }
        result.append(TeamUptime(team_name=team_name, team_number=_team_number(team_name), uptimes=uptimes))
    logger.info(f"Fetched uptimes for {len(result)} teams")
    return result


def _service_checks(data: object) -> list[ServiceCheck]:
    """A service's checks, newest first, with the debug output and error of only the latest ten kept."""
    checks: list[ServiceCheck] = []
    for entry in [] if data is None else _objects(data, "the service checks"):
        round_obj = _object(_field(entry, "Round", "a service check"), "a service check's round")
        checks.append(
            ServiceCheck(
                round_number=_whole(_field(round_obj, "ID", "a service check's round"), "a round number"),
                timestamp=_timestamp(round_obj.get("StartTime", ""), "a round's start time"),
                passed=_boolean(_field(entry, "Result", "a service check"), "a service check's result"),
                debug=_clean_output(entry.get("Debug", ""), "a service check's debug output"),
                error=_clean_output(entry.get("Error", ""), "a service check's error"),
            )
        )
    checks.sort(key=lambda c: c.round_number, reverse=True)
    for old_check in checks[10:]:
        old_check.debug = ""
        old_check.error = ""
    return checks


@lru_cache(maxsize=1)
def get_quotient_client() -> QuotientClient:
    return QuotientClient()
