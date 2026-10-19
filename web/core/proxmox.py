"""Client for the Proxmox VE REST API and Battleship team VM mapping."""

import logging
import re
import ssl
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

CLONE_VMID_BASE = 10000
CLONE_VMID_TEAM_STRIDE = 100
_PROXY_DOWN_STATUSES = frozenset({502, 503, 504, 595, 596, 599})
_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


class ProxmoxAPIError(Exception):
    """Raised when the Proxmox API is unconfigured, unreachable, or returns an error."""


@dataclass(frozen=True)
class ProxmoxVM:
    vmid: int
    name: str
    host: str
    node: str
    status: str


@dataclass(frozen=True)
class ProxmoxSnapshot:
    name: str
    description: str


@dataclass(frozen=True)
class ProxmoxTaskStatus:
    status: str
    exitstatus: str = ""

    @property
    def finished(self) -> bool:
        return self.status == "stopped"

    @property
    def ok(self) -> bool:
        return self.finished and (self.exitstatus == "OK" or self.exitstatus.startswith("WARNINGS"))


def team_pool_id(team_number: int) -> str:
    """Return the Proxmox resource pool ID for `team_number` under Battleship's convention."""
    return f"blue-team-{team_number:02d}"


def is_safe_identifier(value: str) -> bool:
    """True when `value` is a safe Proxmox node or snapshot identifier with no dot-segments."""
    return bool(value and len(value) <= 128 and ".." not in value and _IDENTIFIER_RE.match(value))


def is_valid_snapshot_name(name: str) -> bool:
    """True when `name` is a real Proxmox snapshot name (not the synthetic 'current' state)."""
    return name != "current" and is_safe_identifier(name)


def normalize_ticket_host(team_number: int, raw_hostname: str) -> str | None:
    """Extract the bare machine host from a ticket's hostname for a given team, or None if invalid."""
    if not 1 <= team_number <= 99:
        return None
    candidate = raw_hostname.strip().lower()
    if not candidate:
        return None
    candidate = candidate.removeprefix(f"team{team_number:02d}-")
    host = candidate.split(".", 1)[0]
    if not _HOST_RE.match(host):
        return None
    return host


def classify_team_vm(team_number: int, vmid: int, name: str, *, is_template: bool = False) -> str | None:
    """Return the bare host name when `(vmid, name)` belongs to `team_number` under Battleship's rules, else None."""
    if is_template or not 1 <= team_number <= 99:
        return None
    vmid_min = CLONE_VMID_BASE + team_number * CLONE_VMID_TEAM_STRIDE
    if not vmid_min <= vmid < vmid_min + CLONE_VMID_TEAM_STRIDE:
        return None
    prefix = f"team{team_number:02d}-"
    lowered = name.lower()
    if not lowered.startswith(prefix):
        return None
    host = lowered.removeprefix(prefix)
    if not _HOST_RE.match(host):
        return None
    return host


def match_team_vm(vms: list[ProxmoxVM], team_number: int, raw_hostname: str) -> ProxmoxVM | None:
    """Find the VM in `vms` whose host matches `raw_hostname` for `team_number`."""
    target_host = normalize_ticket_host(team_number, raw_hostname)
    if not target_host:
        return None
    return next((vm for vm in vms if vm.host == target_host), None)


def _is_unavailable(exc: BaseException | None) -> bool:
    """True if Proxmox is down or unreachable (expected outside competition windows)."""
    while exc is not None:
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
            return True
        exc = exc.__cause__
    return False


def _log_failure(message: str, exc: Exception) -> None:
    if _is_unavailable(exc):
        logger.warning(f"{message}: Proxmox unavailable ({str(exc).splitlines()[0]})")
    else:
        logger.exception(f"{message}: {exc}")


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except RecursionError as e:
        raise ValueError("the answer is nested too deep to read") from e


def _clean_text(value: object, what: str, max_len: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} isn't text: {value!r:.80}")
    return _CONTROL_CHARS_RE.sub("", value)[:max_len]


def _whole(value: object, what: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{what} isn't a whole number: {value!r:.80}")
    return value


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


class ProxmoxClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        verify_ssl: bool | None = None,
        ca_file: str | None = None,
    ) -> None:
        base = base_url if base_url is not None else str(getattr(settings, "PROXMOX_API_URL", ""))
        token = api_token if api_token is not None else str(getattr(settings, "PROXMOX_API_TOKEN", ""))
        ca = ca_file if ca_file is not None else str(getattr(settings, "PROXMOX_CA_FILE", ""))
        self.base_urls = [u.strip().rstrip("/") for u in base.split(",") if u.strip()]
        self.base_url = self.base_urls[0] if self.base_urls else ""
        self.api_token = token.strip()
        self.ca_file = ca.strip()
        self.verify_ssl = verify_ssl if verify_ssl is not None else bool(getattr(settings, "PROXMOX_VERIFY_SSL", True))
        self.client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if not self.base_urls or not self.api_token:
            raise ProxmoxAPIError("Proxmox API credentials not configured")
        if self.client is None:
            auth_header = (
                self.api_token if self.api_token.startswith("PVEAPIToken=") else f"PVEAPIToken={self.api_token}"
            )
            verify: ssl.SSLContext | bool = self.verify_ssl
            if self.verify_ssl and self.ca_file:
                ctx = ssl.create_default_context(cafile=self.ca_file)
                # Python 3.13+ enables strict X.509 checks, which reject Proxmox's cluster CA
                # because it has no keyUsage extension. The chain is still verified against it.
                ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
                verify = ctx
            self.client = httpx.Client(
                verify=verify,
                headers={"Authorization": auth_header},
            )
        return self.client

    def _request(self, method: str, endpoint: str, *, timeout: float | None = None) -> httpx.Response:
        client = self._get_client()
        req_timeout = timeout if timeout is not None else float(getattr(settings, "HTTPX_DEFAULT_TIMEOUT", 10))
        is_read = method.upper() == "GET"
        for idx, base_url in enumerate(self.base_urls):
            is_last = idx + 1 == len(self.base_urls)
            try:
                resp = client.request(method, f"{base_url}{endpoint}", timeout=req_timeout)
            except httpx.ConnectError:
                if is_last:
                    raise
                continue
            except httpx.TransportError:
                if is_last or not is_read:
                    raise
                continue
            if is_read and resp.status_code in _PROXY_DOWN_STATUSES and not is_last:
                continue
            return resp
        raise ProxmoxAPIError("Proxmox API credentials not configured")

    def list_team_vms(self, team_number: int) -> list[ProxmoxVM] | None:
        """Fetch the team's VMs from its Proxmox pool, filtered by Battleship naming and VMID rules."""
        if not 1 <= team_number <= 99:
            return []
        pool_id = team_pool_id(team_number)
        try:
            response = self._request("get", f"/api2/json/pools/{quote(pool_id, safe='')}")
            response.raise_for_status()
            data = _object(_field(_json(response), "data", "the pool response"), "the pool data")
            members = _objects(data.get("members", []), "pool members")

            vms: list[ProxmoxVM] = []
            for member in members:
                if member.get("type") != "qemu":
                    continue
                vmid = _whole(_field(member, "vmid", "a VM"), "a VM's vmid")
                name = _clean_text(_field(member, "name", "a VM"), "a VM's name")
                node = _clean_text(_field(member, "node", "a VM"), "a VM's node")
                status_raw = member.get("status", "unknown")
                status = _clean_text(status_raw, "a VM's status") if status_raw is not None else "unknown"
                tmpl_raw = member.get("template", 0)
                if not isinstance(tmpl_raw, int | bool):
                    raise ValueError(f"a VM's template flag isn't a bool or int: {tmpl_raw!r:.80}")
                host = classify_team_vm(team_number, vmid, name, is_template=bool(tmpl_raw))
                if host and is_safe_identifier(node):
                    vms.append(ProxmoxVM(vmid=vmid, name=name, host=host, node=node, status=status))

            return sorted(vms, key=lambda v: v.name)
        except ProxmoxAPIError:
            return None
        except httpx.HTTPError as e:
            _log_failure(f"Failed to list Proxmox VMs for pool {pool_id}", e)
            return None
        except ValueError as e:
            logger.exception(f"Failed to parse Proxmox pool response for {pool_id}: {e}")
            return None

    def list_snapshots(self, node: str, vmid: int) -> list[ProxmoxSnapshot] | None:
        """Fetch the snapshots of a VM on `node`, excluding the synthetic 'current' entry."""
        if not is_safe_identifier(node) or vmid <= 0:
            return []
        try:
            response = self._request("get", f"/api2/json/nodes/{quote(node, safe='')}/qemu/{vmid}/snapshot")
            response.raise_for_status()
            raw_list = _objects(_field(_json(response), "data", "the snapshots response"), "snapshots")

            snapshots: list[ProxmoxSnapshot] = []
            for item in raw_list:
                name = _clean_text(_field(item, "name", "a snapshot"), "a snapshot's name")
                if not is_valid_snapshot_name(name):
                    continue
                desc_raw = item.get("description", "")
                description = _clean_text(desc_raw, "a snapshot's description") if desc_raw is not None else ""
                snapshots.append(ProxmoxSnapshot(name=name, description=description))

            return snapshots
        except ProxmoxAPIError:
            return None
        except httpx.HTTPError as e:
            _log_failure(f"Failed to list snapshots for VM {vmid} on {node}", e)
            return None
        except ValueError as e:
            logger.exception(f"Failed to parse snapshots response for VM {vmid} on {node}: {e}")
            return None

    def rollback_snapshot(self, node: str, vmid: int, snapshot: str) -> str:
        """Roll back `vmid` on `node` to `snapshot`, returning the Proxmox task UPID."""
        if not is_safe_identifier(node) or vmid <= 0 or not is_valid_snapshot_name(snapshot):
            raise ProxmoxAPIError("Invalid node, VMID, or snapshot name")
        try:
            response = self._request(
                "post",
                f"/api2/json/nodes/{quote(node, safe='')}/qemu/{vmid}/snapshot/{quote(snapshot, safe='')}/rollback",
            )
            response.raise_for_status()
            upid = _clean_text(_field(_json(response), "data", "the rollback response"), "the rollback task ID")
            if not upid:
                raise ProxmoxAPIError("Proxmox rollback returned an empty task ID")
            return upid
        except (httpx.HTTPError, ValueError) as e:
            _log_failure(f"Failed to roll back VM {vmid} on {node} to {snapshot}", e)
            raise ProxmoxAPIError(f"Could not roll back VM {vmid} to '{snapshot}'") from e

    def start_vm(self, node: str, vmid: int) -> str:
        """Start `vmid` on `node`, returning the Proxmox task UPID."""
        if not is_safe_identifier(node) or vmid <= 0:
            raise ProxmoxAPIError("Invalid node or VMID")
        try:
            response = self._request("post", f"/api2/json/nodes/{quote(node, safe='')}/qemu/{vmid}/status/start")
            response.raise_for_status()
            upid = _clean_text(_field(_json(response), "data", "the start response"), "the start task ID")
            if not upid:
                raise ProxmoxAPIError("Proxmox start returned an empty task ID")
            return upid
        except (httpx.HTTPError, ValueError) as e:
            _log_failure(f"Failed to start VM {vmid} on {node}", e)
            raise ProxmoxAPIError(f"Could not start VM {vmid}") from e

    def get_task_status(self, node: str, upid: str) -> ProxmoxTaskStatus:
        """Read the status of a Proxmox task `upid` on `node`."""
        if not is_safe_identifier(node) or not upid or "/" in upid or ".." in upid:
            raise ProxmoxAPIError("Invalid node or task ID")
        try:
            response = self._request(
                "get",
                f"/api2/json/nodes/{quote(node, safe='')}/tasks/{quote(upid, safe='')}/status",
            )
            response.raise_for_status()
            data = _object(_field(_json(response), "data", "the task status response"), "the task status")
            status = _clean_text(_field(data, "status", "the task status"), "the task status")
            exit_raw = data.get("exitstatus", "")
            exitstatus = _clean_text(exit_raw, "the task exitstatus") if exit_raw is not None else ""
            return ProxmoxTaskStatus(status=status, exitstatus=exitstatus)
        except (httpx.HTTPError, ValueError) as e:
            _log_failure(f"Failed to read Proxmox task status {upid} on {node}", e)
            raise ProxmoxAPIError("Could not read Proxmox task status") from e

    def wait_for_task(
        self,
        node: str,
        upid: str,
        *,
        timeout_seconds: float = 120.0,
        poll_interval: float = 1.0,
        allow_already_running: bool = False,
    ) -> ProxmoxTaskStatus:
        """Poll `get_task_status` until the task stops or `timeout_seconds` elapses."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            task = self.get_task_status(node, upid)
            if task.finished:
                if task.ok or (allow_already_running and "already running" in task.exitstatus.lower()):
                    return task
                raise ProxmoxAPIError(f"Proxmox task failed: {task.exitstatus or 'unknown error'}")
            if time.monotonic() >= deadline:
                raise ProxmoxAPIError("Timed out waiting for Proxmox task to complete")
            time.sleep(poll_interval)


def get_proxmox_client() -> ProxmoxClient:
    return ProxmoxClient()
