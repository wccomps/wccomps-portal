"""Authentik API manager for application control."""

import logging
from typing import TypedDict, cast
from urllib.parse import quote

import httpx
from django.conf import settings


class AuthentikApplication(TypedDict):
    pk: str
    slug: str


class AuthentikBinding(TypedDict):
    pk: str
    enabled: bool


class AuthentikUser(TypedDict):
    pk: int
    username: str


class AuthentikGroup(TypedDict):
    pk: str
    name: str


class AuthentikGroupEntry(AuthentikGroup):
    parents: list[str]


class AuthentikUserEntry(TypedDict):
    uid: str
    is_active: bool
    groups: list[str]


class AuthentikGroupName(TypedDict):
    name: str


class AuthentikUserWithGroups(AuthentikUser):
    groups_obj: list[AuthentikGroupName]


logger = logging.getLogger(__name__)

MAX_PAGES = 100  # of 100 results each: a list endpoint still saying "next" past this is broken, not long


def _holds(value: object, kind: type) -> bool:
    """Whether a JSON value matches kind: list means list[str]; exact type check so bool does not match int."""
    if kind is list:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return type(value) is kind


class AuthentikAPIError(Exception):
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


class AuthentikManager:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url or getattr(settings, "AUTHENTIK_URL", "")
        self.token = api_token or getattr(settings, "AUTHENTIK_TOKEN", "")
        self.client = client or httpx.Client(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=settings.HTTPX_DEFAULT_TIMEOUT,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> AuthentikManager:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _log_request(self, method: str, url: str, **kwargs: object) -> None:
        """Log HTTP request details (without sensitive headers)."""
        headers = kwargs.get("headers")
        safe_headers = {k: v for k, v in headers.items() if k != "Authorization"} if isinstance(headers, dict) else {}
        logger.debug(
            f"Authentik API Request: {method} {url} | Headers: {safe_headers} | "
            f"Params: {kwargs.get('params')} | Data: {kwargs.get('json')}"
        )

    def _handle_response_error(self, response: httpx.Response, context: str) -> AuthentikAPIError:
        try:
            error_data = response.json()
        except ValueError, RecursionError:
            error_data = None
        detail = error_data.get("detail") if isinstance(error_data, dict) else None
        error_detail = detail if isinstance(detail, str) else response.text

        status_messages = {
            401: "Authentication failed (check AUTHENTIK_TOKEN)",
            403: "Permission denied (the token lacks required permissions)",
            404: "Resource not found",
            429: "Rate limit exceeded",
            500: "Authentik server error",
            502: "Bad gateway (Authentik may be down)",
            503: "Service unavailable (Authentik may be overloaded)",
        }

        status_msg = status_messages.get(response.status_code, "HTTP error")
        message = f"{context}: {status_msg}"

        return AuthentikAPIError(
            message=message,
            status_code=response.status_code,
            response_text=error_detail,
            url=str(response.url),
        )

    def _json(self, response: httpx.Response, context: str) -> object:
        """The answer's JSON; AuthentikAPIError when it isn't JSON (or is nested too deep to read)."""
        try:
            return response.json()
        except ValueError, RecursionError:
            raise AuthentikAPIError(
                f"{context}: the answer isn't JSON", response.status_code, url=str(response.url)
            ) from None

    def _results(self, response: httpx.Response, context: str) -> tuple[list[dict[str, object]], bool]:
        """A list endpoint's results (objects only) and whether another page follows; AuthentikAPIError for an
        answer of any other shape."""
        data = self._json(response, context)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise AuthentikAPIError(f"{context}: the answer has no list of results", url=str(response.url))
        pagination = data.get("pagination") if isinstance(data, dict) else None
        has_next = bool(pagination.get("next")) if isinstance(pagination, dict) else False
        return results, has_next

    def _first_user(self, response: httpx.Response, context: str) -> AuthentikUser | None:
        """The first user a users query found, or None; AuthentikAPIError if it has no int pk, or a username that
        isn't text."""
        users, _ = self._results(response, context)
        if not users:
            return None
        user = users[0]
        if type(user.get("pk")) is not int or not isinstance(user.get("username", ""), str):
            raise AuthentikAPIError(
                f"{context}: the user has no pk, or a username that isn't text", url=str(response.url)
            )
        return cast(AuthentikUser, user)

    def list_applications(self) -> list[str]:
        """App slugs for the add-app picker, via /providers/all/.

        This lists every app that has a provider, regardless of which apps the service account may
        open (the application list would return only those). It needs the service account's role to
        hold the "Can view provider" global permission (authentik_core.view_provider); without it the
        request 403s and this returns nothing.
        """
        url = f"{self.base_url}/api/v3/providers/all/"
        slugs: set[str] = set()
        try:
            for page in range(1, MAX_PAGES + 1):
                params: dict[str, str | int] = {"page_size": 100, "page": page}
                self._log_request("GET", url, params=params)
                response = self.client.get(url, params=params)
                response.raise_for_status()
                providers, has_next = self._results(response, "List providers")
                for provider in providers:
                    slug = provider.get("assigned_application_slug")
                    if isinstance(slug, str) and slug:
                        slugs.add(slug)
                if not has_next:
                    break
            else:
                raise AuthentikAPIError(f"List providers: more than {MAX_PAGES} pages")
            logger.info(f"Found {len(slugs)} application slugs via providers")
            return sorted(slugs)
        except Exception as e:
            logger.exception(f"Failed to list applications via providers: {e}")
            return []

    def get_application_by_slug(self, slug: str) -> AuthentikApplication | None:
        """Get application details by exact slug, via the retrieve endpoint.

        The list endpoint ignores ?slug= and, for non-superusers, returns only a per-user cached
        "allowed" list, which can omit apps the service account can access.
        """
        url = f"{self.base_url}/api/v3/core/applications/{quote(slug, safe='')}/"
        try:
            self._log_request("GET", url)
            response = self.client.get(url)
            if response.status_code == 404:
                logger.warning(f"Application '{slug}' not found in Authentik")
                return None
            response.raise_for_status()
            app = self._json(response, f"Get application '{slug}'")
            if not isinstance(app, dict) or not isinstance(app.get("pk"), str):
                logger.error(f"Application '{slug}': Authentik's answer has no pk")
                return None
            logger.info(f"Found application '{slug}' with pk={app['pk']}")
            return cast(AuthentikApplication, app)
        except httpx.HTTPStatusError as e:
            error = self._handle_response_error(e.response, f"Get application '{slug}'")
            logger.exception(str(error))
            return None
        except httpx.HTTPError as e:
            logger.exception(f"Network error getting application '{slug}': {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting application '{slug}': {e}", exc_info=True)
            return None

    def get_blueteam_binding(self, app_pk: str) -> tuple[AuthentikBinding | None, str | None]:
        """Find the application's BlueTeam group binding, returning (binding, error_message)."""
        url = f"{self.base_url}/api/v3/policies/bindings/"
        try:
            logger.debug(f"Querying bindings for application {app_pk}")
            self._log_request("GET", url, params={"target": app_pk})

            response = self.client.get(url, params={"target": app_pk})
            response.raise_for_status()
            bindings, _ = self._results(response, f"Query bindings for app {app_pk}")

            logger.debug(f"Found {len(bindings)} binding(s) for application {app_pk}")

            for binding in bindings:
                group_obj = binding.get("group_obj")
                binding_pk = binding.get("pk")
                group_name = group_obj.get("name") if isinstance(group_obj, dict) else None

                if isinstance(group_name, str) and isinstance(binding_pk, str):
                    logger.debug(f"Found group binding: {group_name} (pk={binding_pk})")

                    if "blueteam" in group_name.lower():
                        logger.info(f"Found blueteam group binding: {binding_pk} (group={group_name})")
                        return cast(AuthentikBinding, binding), None

            error_msg = (
                f"No BlueTeam group binding found for application {app_pk}. "
                f"Found {len(bindings)} binding(s) but none matched."
            )
            logger.error(error_msg)
            return None, error_msg

        except httpx.HTTPStatusError as e:
            error = self._handle_response_error(e.response, f"Query bindings for app {app_pk}")
            logger.exception(f"Failed to query bindings: {error}")
            return None, str(error)
        except httpx.HTTPError as e:
            error_msg = f"Network error querying bindings: {e}"
            logger.exception(error_msg)
            return None, error_msg
        except Exception as e:
            error_msg = f"Unexpected error querying bindings: {e}"
            logger.error(error_msg, exc_info=True)
            return None, error_msg

    def update_binding_enabled(self, binding: AuthentikBinding, enabled: bool) -> bool:
        """Set a binding's enabled state; an enabled binding allows the group access."""
        try:
            binding_pk = binding["pk"]

            # Modify the enabled field and PUT the entire object back
            binding["enabled"] = enabled

            response = self.client.put(
                f"{self.base_url}/api/v3/policies/bindings/{binding_pk}/",
                json=binding,
            )
            response.raise_for_status()
            state = "enabled" if enabled else "disabled"
            logger.info(f"Set binding {binding_pk} to {state}")
            return True
        except httpx.HTTPStatusError as e:
            # Log Authentik's response body (why the 400 etc.), not just the status code
            logger.exception(str(self._handle_response_error(e.response, f"Update binding {binding_pk}")))
            return False
        except Exception as e:
            logger.exception(f"Failed to update binding {binding.get('pk')}: {e}")
            return False

    def _toggle_application(self, app_slug: str, *, enable: bool) -> tuple[bool, str | None]:
        """Enable or disable an application for blue teams via its BlueTeam group binding."""
        action = "enable" if enable else "disable"
        try:
            logger.info(f"Attempting to {action} application '{app_slug}'")

            app = self.get_application_by_slug(app_slug)
            if not app:
                error_msg = f"Application '{app_slug}' not found in Authentik"
                logger.error(error_msg)
                return False, error_msg

            app_pk = app["pk"]
            logger.debug(f"Application '{app_slug}' has pk={app_pk}")

            binding, binding_error = self.get_blueteam_binding(app_pk)
            if not binding:
                error_msg = f"Could not find BlueTeam group binding: {binding_error}"
                logger.error(error_msg)
                return False, error_msg

            logger.debug(f"Using binding pk={binding['pk']}")

            success = self.update_binding_enabled(binding, enabled=enable)

            if success:
                state = "enabled" if enable else "disabled"
                logger.info(f"Application '{app_slug}' {state} for blue teams")
                return True, None
            error_msg = f"Failed to {action} binding for application '{app_slug}'"
            logger.error(error_msg)
            return False, error_msg

        except Exception as e:
            error_msg = f"Unexpected error {action}ing application '{app_slug}': {e!s}"
            logger.error(error_msg, exc_info=True)
            return False, error_msg

    def enable_application(self, app_slug: str) -> tuple[bool, str | None]:
        """Enable application for blue teams by enabling the BlueTeam group binding."""
        return self._toggle_application(app_slug, enable=True)

    def disable_application(self, app_slug: str) -> tuple[bool, str | None]:
        """Disable application for blue teams by disabling the BlueTeam group binding."""
        return self._toggle_application(app_slug, enable=False)

    def update_user_discord_id(self, username: str, discord_id: int, uid: str) -> bool:
        """Store Discord ID in Authentik user attributes, preserving existing attributes.

        Nothing is written unless the user found by username has this uid (OIDC sub): a stale
        username may since belong to someone else.
        """
        try:
            response = self.client.get(
                f"{self.base_url}/api/v3/core/users/?username={quote(username, safe='')}",
            )
            response.raise_for_status()
            user = self._first_user(response, f"Find user {username}")

            if not user:
                logger.warning(f"Authentik user not found for username {username}")
                return False

            if user.get("uid") != uid:
                logger.warning(f"Authentik user {username} is a different identity now; not storing discord_id")
                return False
            user_pk = user["pk"]

            existing_attrs = user.get("attributes", {})
            attributes: dict[str, object] = dict(existing_attrs) if isinstance(existing_attrs, dict) else {}
            attributes["discord_id"] = str(discord_id)

            response = self.client.patch(
                f"{self.base_url}/api/v3/core/users/{user_pk}/",
                json={"attributes": attributes},
            )
            response.raise_for_status()
            logger.info(f"Updated discord_id for Authentik user {username} (pk={user_pk})")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                logger.exception(
                    f"Authentik API token lacks permission to update user {username}. Error: {e.response.text}"
                )
            else:
                logger.exception(f"Failed to update Authentik user discord_id: {e}")
            raise
        except Exception as e:
            logger.exception(f"Failed to update discord_id for user {username}: {e}")
            return False

    def revoke_user_sessions(self, username: str) -> tuple[bool, str | None, int]:
        """Revoke all active sessions for a user, returning (success, error_message, sessions_revoked)."""
        try:
            response = self.client.get(
                f"{self.base_url}/api/v3/core/users/",
                params={"username": username},
            )
            response.raise_for_status()
            user = self._first_user(response, f"Find user {username}")

            if not user:
                return False, f"User {username} not found", 0

            user_pk = user["pk"]
            logger.info(f"Found user {username} with pk={user_pk}")

            response = self.client.get(
                f"{self.base_url}/api/v3/core/authenticated_sessions/",
                params={"user": user_pk},
            )
            response.raise_for_status()
            sessions, _ = self._results(response, f"List sessions of {username}")

            logger.info(f"Found {len(sessions)} session(s) for user {username}")

            revoked_count = 0
            for session in sessions:
                session_uuid = session.get("uuid")
                if not isinstance(session_uuid, str) or not session_uuid:
                    continue

                try:
                    response = self.client.delete(
                        f"{self.base_url}/api/v3/core/authenticated_sessions/{session_uuid}/",
                    )
                    response.raise_for_status()
                    revoked_count += 1
                    logger.info(f"Revoked session {session_uuid} for user {username}")
                except Exception as e:
                    logger.warning(f"Failed to revoke session {session_uuid}: {e}")

            return True, None, revoked_count

        except httpx.HTTPStatusError as e:
            error = self._handle_response_error(e.response, f"Revoke sessions for user {username}")
            logger.exception(str(error))
            return False, str(error), 0
        except httpx.HTTPError as e:
            error_msg = f"Network error revoking sessions: {e}"
            logger.exception(error_msg)
            return False, error_msg, 0
        except Exception as e:
            error_msg = f"Unexpected error revoking sessions: {e}"
            logger.error(error_msg, exc_info=True)
            return False, error_msg, 0

    def toggle_user(self, username: str, is_active: bool) -> tuple[bool, str]:
        """Enable or disable a team account in Authentik, refusing accounts that aren't team accounts."""
        from core.authentik_utils import validate_team_account

        try:
            response = self.client.get(
                f"{self.base_url}/api/v3/core/users/?username={quote(username, safe='')}",
            )
            response.raise_for_status()
            user = self._first_user(response, f"Find user {username}")

            if not user:
                return (False, "User not found")

            is_valid, error = validate_team_account(user, username)
            if not is_valid:
                return (False, error)

            response = self.client.patch(
                f"{self.base_url}/api/v3/core/users/{user['pk']}/",
                json={"is_active": is_active},
            )
            response.raise_for_status()
            return (True, "")
        except Exception as e:
            logger.exception(f"Failed to toggle {username}: {e}")
            return (False, "Account toggle failed. Check the server logs.")

    def reset_blueteam_password(self, team_number: int, password: str) -> tuple[bool, str]:
        """Reset a blue team account's password in Authentik.

        Leaves is_active alone: team accounts are enabled only while the competition runs.
        """
        from core.authentik_utils import validate_team_account
        from team.models import MAX_TEAMS, team_username

        if team_number < 1 or team_number > MAX_TEAMS:
            return (False, f"Team number must be between 1 and {MAX_TEAMS}")

        username = team_username(team_number)

        try:
            response = self.client.get(
                f"{self.base_url}/api/v3/core/users/?username={quote(username, safe='')}",
            )
            response.raise_for_status()
            user = self._first_user(response, f"Find user {username}")

            if not user:
                return (False, f"User {username} not found")

            user_pk: int = user["pk"]

            is_valid, error = validate_team_account(user, username)
            if not is_valid:
                return (False, error)

            response = self.client.post(
                f"{self.base_url}/api/v3/core/users/{user_pk}/set_password/",
                json={"password": password},
            )
            response.raise_for_status()

            return (True, "")

        except Exception as e:
            logger.exception(f"Failed to reset password for {username}: {e}")
            return (False, "see the server logs")  # the callers say what failed

    def _list_all(self, path: str, params: dict[str, str], fields: dict[str, type]) -> list[dict[str, object]]:
        """Fetch every page of an Authentik list endpoint, each result holding these fields of these types (a list
        field: of strings). Raises AuthentikAPIError on any HTTP error or an answer of another shape."""
        results: list[dict[str, object]] = []
        for page in range(1, MAX_PAGES + 1):
            response = self.client.get(
                f"{self.base_url}{path}", params={**params, "page": str(page), "page_size": "100"}
            )
            if response.is_error:
                raise self._handle_response_error(response, f"List {path}")
            found, has_next = self._results(response, f"List {path}")
            for result in found:
                if not all(_holds(result.get(name), kind) for name, kind in fields.items()):
                    raise AuthentikAPIError(f"List {path}: a result without {', '.join(fields)}", url=str(response.url))
            results.extend(found)
            if not has_next:
                return results
        raise AuthentikAPIError(f"List {path}: more than {MAX_PAGES} pages")

    def list_all_users(self) -> list[AuthentikUserEntry]:
        """Every Authentik user, with ``uid``, ``is_active`` and direct group pks in ``groups``."""
        return cast(
            list[AuthentikUserEntry],
            self._list_all(
                "/api/v3/core/users/", {"include_groups": "false"}, {"uid": str, "is_active": bool, "groups": list}
            ),
        )

    def list_all_groups(self) -> list[AuthentikGroupEntry]:
        """Every Authentik group, with ``pk``, ``name`` and parent pks in ``parents``."""
        return cast(
            list[AuthentikGroupEntry],
            self._list_all(
                "/api/v3/core/groups/", {"include_users": "false"}, {"pk": str, "name": str, "parents": list}
            ),
        )

    def get_user_with_groups(self, username: str) -> AuthentikUserWithGroups | None:
        """Get an Authentik user with their groups_obj, or None if not found or the lookup failed (logged)."""
        try:
            response = self.client.get(
                f"{self.base_url}/api/v3/core/users/",
                params={"username": username},
            )
            response.raise_for_status()
            user = self._first_user(response, f"Find user {username}")
            if user is None:
                return None
            raw = cast(dict[str, object], user)
            groups = raw.setdefault("groups_obj", [])
            if not isinstance(groups, list) or not all(
                isinstance(g, dict) and isinstance(g.get("name"), str) for g in groups
            ):
                raise AuthentikAPIError(f"Find user {username}: groups without names", url=str(response.url))
            return cast(AuthentikUserWithGroups, raw)
        except Exception as e:
            logger.exception(f"Failed to get user {username}: {e}")
            return None

    def get_group_by_name(self, name: str) -> AuthentikGroup | None:
        """Look up an Authentik group by exact name, or None if not found."""
        try:
            response = self.client.get(
                f"{self.base_url}/api/v3/core/groups/",
                params={"name": name},
            )
            response.raise_for_status()
            groups, _ = self._results(response, f"Find group {name}")
            found = next((g for g in groups if g.get("name") == name and isinstance(g.get("pk"), str)), None)
            return cast(AuthentikGroup | None, found)
        except Exception as e:
            logger.exception(f"Failed to look up group {name}: {e}")
            return None

    def add_user_to_group(self, user_pk: int, group_pk: str) -> tuple[bool, str]:
        """Add a user to an Authentik group."""
        try:
            response = self.client.post(
                f"{self.base_url}/api/v3/core/groups/{group_pk}/add_user/",
                json={"pk": user_pk},
            )
            response.raise_for_status()
            return (True, "")
        except httpx.HTTPStatusError as e:
            error = self._handle_response_error(e.response, f"Add user {user_pk} to group {group_pk}")
            logger.exception(str(error))
            return (False, str(error))
        except Exception as e:
            logger.exception(f"Failed to add user {user_pk} to group {group_pk}: {e}")
            return (False, f"Failed to add user to group: {e}")
