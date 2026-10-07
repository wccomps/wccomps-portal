"""Client for Quotient scoring engine REST API."""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


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


def _team_number(team_name: str) -> int:
    """Portal team number from a Quotient team name ("team09" -> 9); 0 if the name has no digits."""
    return int("".join(c for c in team_name if c.isdigit()) or "0")


@dataclass
class QuotientService:
    """Represents a service on a box."""

    name: str
    display_name: str
    type: str  # custom, dns, smtp, imap, ssh, web, pop3


@dataclass
class QuotientBox:
    name: str
    ip: str
    services: list[QuotientService]


@dataclass
class QuotientInfrastructure:
    boxes: list[QuotientBox]
    event_name: str
    team_count: int
    api_version: str


@dataclass
class TeamScore:
    team_name: str
    team_number: int
    total_score: float
    score_history: list[dict[str, int | float]]  # [{Round: int, Total: float}, ...]


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
    open_time: str | None
    due_time: str | None
    close_time: str | None
    files: list[str]
    submissions: list[dict[str, str | int]]  # [{TeamID, InjectID, SubmissionTime, ...}, ...]


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

    def get_infrastructure(self, force_refresh: bool = False) -> QuotientInfrastructure | None:
        """Fetch infrastructure from Quotient API, or None if unavailable."""
        cache_key = "quotient_infrastructure"

        if not force_refresh:
            cached: QuotientInfrastructure | None = cache.get(cache_key)
            if cached:
                logger.debug("Returning cached infrastructure")
                return cached

        try:
            response = self._request("get", "/api/metadata")
            response.raise_for_status()

            data = response.json()

            # /api/metadata returns: {"boxes": [{"name": str, "ip": str, "services": [str]}]}
            boxes = []
            for box_data in data.get("boxes", []):
                # Services are just strings (display names) in metadata endpoint
                services = [
                    QuotientService(
                        name=svc_name,
                        display_name=svc_name,
                        type="custom",  # Type not provided by metadata endpoint
                    )
                    for svc_name in box_data.get("services", [])
                ]

                boxes.append(
                    QuotientBox(
                        name=box_data["name"],
                        ip=box_data["ip"],
                        services=services,
                    )
                )

            infrastructure = QuotientInfrastructure(
                boxes=boxes,
                event_name="",  # Not provided by metadata endpoint
                team_count=0,  # Not provided by metadata endpoint
                api_version="v1",
            )

            cache.set(cache_key, infrastructure, self.cache_ttl)
            logger.info(f"Fetched {len(boxes)} boxes from Quotient API")

            return infrastructure

        except (httpx.HTTPError, QuotientAPIError) as e:
            _log_failure("Failed to fetch infrastructure from Quotient", e)
            return None
        except (KeyError, ValueError) as e:
            logger.exception(f"Failed to parse infrastructure response: {e}")
            return None

    def get_scores(self, force_refresh: bool = False) -> list[TeamScore] | None:
        """Fetch team scores from Quotient API, or None if unavailable."""
        cache_key = "quotient_scores"

        if not force_refresh:
            cached: list[TeamScore] | None = cache.get(cache_key)
            if cached:
                return cached

        try:
            response = self._request("get", "/api/graphs/scores")
            response.raise_for_status()

            data = response.json()
            scores = []
            for team_data in data.get("series", []):
                team_name = team_data["Name"]
                history = team_data.get("Data", [])
                total = history[-1]["Total"] if history else 0

                scores.append(
                    TeamScore(
                        team_name=team_name,
                        team_number=_team_number(team_name),
                        total_score=float(total),
                        score_history=history,
                    )
                )

            cache.set(cache_key, scores, self.cache_ttl)
            logger.info(f"Fetched scores for {len(scores)} teams")
            return scores

        except (httpx.HTTPError, QuotientAPIError) as e:
            _log_failure("Failed to fetch scores from Quotient", e)
            return None
        except (KeyError, ValueError) as e:
            logger.exception(f"Failed to parse scores response: {e}")
            return None

    def get_injects(self, force_refresh: bool = False) -> list[Inject] | None:
        """Fetch injects from Quotient API, or None if unavailable."""
        cache_key = "quotient_injects"

        if not force_refresh:
            cached: list[Inject] | None = cache.get(cache_key)
            if cached:
                return cached

        try:
            response = self._request("get", "/api/injects")
            response.raise_for_status()

            data = response.json()
            # API returns a list directly
            inject_list = data if isinstance(data, list) else data.get("injects", [])

            injects = [
                Inject(
                    inject_id=i["ID"],
                    title=i["Title"],
                    description=i["Description"],
                    open_time=i.get("OpenTime"),
                    due_time=i.get("DueTime"),
                    close_time=i.get("CloseTime"),
                    files=i.get("InjectFileNames", []),
                    submissions=i.get("Submissions", []),
                )
                for i in inject_list
            ]

            cache.set(cache_key, injects, 60)
            logger.info(f"Fetched {len(injects)} injects")
            return injects

        except (httpx.HTTPError, QuotientAPIError) as e:
            _log_failure("Failed to fetch injects from Quotient", e)
            return None
        except (KeyError, ValueError) as e:
            logger.exception(f"Failed to parse injects response: {e}")
            return None

    def get_service_export(self, force_refresh: bool = False) -> list[TeamServiceExport] | None:
        """Fetch per-service score breakdown from Quotient, or None if unavailable."""
        cache_key = "quotient_service_export"

        if not force_refresh:
            cached: list[TeamServiceExport] | None = cache.get(cache_key)
            if cached:
                return cached

        try:
            response = self._request("get", "/api/engine/export/scores")
            response.raise_for_status()

            data = response.json()
            exports = []
            for team_data in data:
                services = [
                    ServiceExportEntry(
                        service_name=s["service_name"],
                        service_points=s["service_points"],
                        sla_violations=s.get("sla_violations", 0),
                        sla_penalty=s.get("sla_penalty", 0),
                    )
                    for s in team_data.get("services", [])
                ]
                exports.append(
                    TeamServiceExport(
                        team_name=team_data["team_name"],
                        team_number=_team_number(team_data["team_name"]),
                        services=services,
                        gross_points=team_data.get("gross_points", 0),
                        total_sla_penalty=team_data.get("total_sla_penalty", 0),
                        total_points=team_data.get("total_points", 0),
                    )
                )

            cache.set(cache_key, exports, self.cache_ttl)
            logger.info(f"Fetched service export for {len(exports)} teams")
            return exports

        except (httpx.HTTPError, QuotientAPIError) as e:
            _log_failure("Failed to fetch service export", e)
            return None
        except (KeyError, ValueError) as e:
            logger.exception(f"Failed to parse service export: {e}")
            return None

    def get_uptimes(self, force_refresh: bool = False) -> list[TeamUptime] | None:
        """Fetch per-service uptime percentages from Quotient, or None if unavailable."""
        cache_key = "quotient_uptimes"

        if not force_refresh:
            cached: list[TeamUptime] | None = cache.get(cache_key)
            if cached:
                return cached

        try:
            response = self._request("get", "/api/graphs/uptimes")
            response.raise_for_status()

            data = response.json()
            result = []
            for team_data in data.get("series", []):
                team_name = team_data["Name"]
                uptimes = {entry["Service"]: entry["Uptime"] for entry in team_data.get("Data", [])}
                result.append(
                    TeamUptime(
                        team_name=team_name,
                        team_number=_team_number(team_name),
                        uptimes=uptimes,
                    )
                )

            cache.set(cache_key, result, self.cache_ttl)
            logger.info(f"Fetched uptimes for {len(result)} teams")
            return result

        except (httpx.HTTPError, QuotientAPIError) as e:
            _log_failure("Failed to fetch uptimes", e)
            return None
        except (KeyError, ValueError) as e:
            logger.exception(f"Failed to parse uptimes: {e}")
            return None

    def get_service_choices(self) -> list[dict[str, str]]:
        """Service choices for the ticket dropdown, as dicts with 'value', 'label' and 'box_ip' keys."""
        infrastructure = self.get_infrastructure()
        if not infrastructure:
            return []

        choices = [
            {
                "value": f"{box.name}:{service.name}",
                "label": f"{box.name} - {service.display_name}",
                "box_ip": box.ip,
                "box_name": box.name,
                "service_name": service.name,
                "service_type": service.type,
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


@lru_cache(maxsize=1)
def get_quotient_client() -> QuotientClient:
    return QuotientClient()
