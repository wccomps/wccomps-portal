"""Nextcloud user_oidc OCS API manager for blue team account provisioning and teardown."""

import logging
from typing import cast
from urllib.parse import quote

import httpx
from django.conf import settings

from team.models import MAX_TEAMS, team_username

logger = logging.getLogger(__name__)


def nextcloud_configured() -> bool:
    """Whether Nextcloud admin API credentials are configured."""
    return bool(settings.NEXTCLOUD_URL.strip() and settings.NEXTCLOUD_USERNAME.strip() and settings.NEXTCLOUD_PASSWORD)


def reason_for(error: NextcloudAPIError) -> str:
    """Why a Nextcloud call failed, for an operator: never the URL or the answer."""
    return error.message


def _team_account(team_number: int) -> tuple[str, str]:
    """The team's Nextcloud username and display name; NextcloudAPIError outside 1..MAX_TEAMS."""
    if not 1 <= team_number <= MAX_TEAMS:
        raise NextcloudAPIError(f"Team number must be between 1 and {MAX_TEAMS}")
    return team_username(team_number), f"Blue Team {team_number:02d}"


class NextcloudAPIError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_text: str | None = None,
        url: str | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.response_text = response_text
        self.url = url
        super().__init__(self.formatted_message())

    def formatted_message(self) -> str:
        parts = [self.message]
        if self.status_code:
            parts.append(f"Status: {self.status_code}")
        if self.url:
            parts.append(f"URL: {self.url}")
        if self.response_text:
            parts.append(f"Response: {self.response_text[:500]}")
        return " | ".join(parts)


class NextcloudManager:
    """Requests go through _request, so an unreachable Nextcloud raises NextcloudAPIError like a refused answer."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        provider_id: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        raw_url = base_url if base_url is not None else settings.NEXTCLOUD_URL
        self.base_url = raw_url.strip().rstrip("/")
        self.username = (username if username is not None else settings.NEXTCLOUD_USERNAME).strip()
        self.password = password if password is not None else settings.NEXTCLOUD_PASSWORD
        self.provider_id = provider_id if provider_id is not None else settings.NEXTCLOUD_OIDC_PROVIDER_ID
        self.client = client or httpx.Client(
            auth=(self.username, self.password),
            headers={
                "OCS-APIRequest": "true",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=settings.HTTPX_DEFAULT_TIMEOUT,
        )

    def _request(self, verb: str, url: str, **kwargs: object) -> httpx.Response:
        if not self.base_url or not self.username or not self.password:
            raise NextcloudAPIError("Nextcloud API credentials not configured")
        try:
            return cast(httpx.Response, getattr(self.client, verb)(url, **kwargs))
        except httpx.HTTPError as e:
            raise NextcloudAPIError(f"Network error ({type(e).__name__})") from e

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> NextcloudManager:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _handle_response_error(self, response: httpx.Response, context: str) -> NextcloudAPIError:
        status_messages = {
            401: "Authentication failed (check NEXTCLOUD_USERNAME and NEXTCLOUD_PASSWORD)",
            403: "Permission denied",
            404: "Resource not found",
            429: "Rate limit exceeded",
            500: "Nextcloud server error",
            502: "Bad gateway (Nextcloud may be down)",
            503: "Service unavailable (Nextcloud may be overloaded)",
        }
        status_msg = status_messages.get(response.status_code, f"HTTP {response.status_code}")
        return NextcloudAPIError(
            message=f"{context}: {status_msg}",
            status_code=response.status_code,
            response_text=response.text,
            url=str(response.url),
        )

    def _check(self, response: httpx.Response, context: str) -> None:
        """NextcloudAPIError unless the answer is 2xx (a redirect is a failure too)."""
        if not response.is_success:
            raise self._handle_response_error(response, context)

    def _json(self, response: httpx.Response, context: str) -> object:
        """The answer's JSON; NextcloudAPIError when it isn't JSON (or is nested too deep to read)."""
        try:
            return response.json()
        except ValueError, RecursionError:
            raise NextcloudAPIError(
                f"{context}: the answer isn't JSON", response.status_code, url=str(response.url)
            ) from None

    def _user_id(self, response: httpx.Response, context: str) -> str:
        """The user_id in a 2xx OCS answer; NextcloudAPIError for any other status or shape."""
        self._check(response, context)
        data = self._json(response, context)
        ocs = data.get("ocs") if isinstance(data, dict) else None
        payload = ocs.get("data") if isinstance(ocs, dict) else None
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise NextcloudAPIError(
                f"{context}: the answer has no user_id", response.status_code, url=str(response.url)
            )
        return user_id

    def provision_team_user(self, team_number: int) -> None:
        """Pre-provision the team's Nextcloud user_oidc account and home folder (idempotent)."""
        username, display_name = _team_account(team_number)
        context = f"Provision Nextcloud user {username}"
        response = self._request(
            "post",
            f"{self.base_url}/ocs/v2.php/apps/user_oidc/api/v1/user",
            json={"providerId": self.provider_id, "userId": username, "displayName": display_name},
        )
        if self._user_id(response, context) != username:
            raise NextcloudAPIError(
                f"{context}: Nextcloud answered a different user", response.status_code, url=str(response.url)
            )
        logger.info(f"Provisioned Nextcloud user {username}")

    def delete_team_user(self, team_number: int) -> bool:
        """Delete the team's Nextcloud user_oidc account and files; False when it did not exist (404)."""
        username, _ = _team_account(team_number)
        context = f"Delete Nextcloud user {username}"
        response = self._request(
            "delete",
            f"{self.base_url}/ocs/v2.php/apps/user_oidc/api/v1/user/{quote(username, safe='')}",
        )
        if response.status_code == 404:
            return False
        if self._user_id(response, context) != username:
            raise NextcloudAPIError(
                f"{context}: Nextcloud answered a different user", response.status_code, url=str(response.url)
            )
        logger.info(f"Deleted Nextcloud user {username}")
        return True
