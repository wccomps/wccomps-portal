"""Authentik API manager for application control."""

import functools
import logging
import uuid
from collections.abc import Callable
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

SESSIONS_HIDDEN = (
    "Authentik showed the portal's token no sessions at all: its Authentik user lacks the view Authenticated "
    "Session permission, or nobody is signed in to Authentik."
)


def reason_for(error: AuthentikAPIError) -> str:
    """Why an Authentik call failed, for an operator: never the URL or the answer."""
    return error.message


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


def _asks_authentik[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Authentik that can't be reached raises AuthentikAPIError too, so callers catch one type."""

    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except httpx.HTTPError as e:
            raise AuthentikAPIError(f"Network error ({type(e).__name__})") from e

    return wrapper


class AuthentikManager:
    """Each call that asks Authentik raises AuthentikAPIError when Authentik can't be reached or answers other than
    its docstring describes."""

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

        status_msg = status_messages.get(response.status_code, f"HTTP {response.status_code}")
        message = f"{context}: {status_msg}"

        return AuthentikAPIError(
            message=message,
            status_code=response.status_code,
            response_text=error_detail,
            url=str(response.url),
        )

    def _check(self, response: httpx.Response, context: str) -> None:
        """AuthentikAPIError unless the answer is 2xx (a redirect is a failure too)."""
        if not response.is_success:
            raise self._handle_response_error(response, context)

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
        answer other than 2xx (a redirect too) or of any other shape."""
        self._check(response, context)
        data = self._json(response, context)
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise AuthentikAPIError(f"{context}: the answer has no list of results", url=str(response.url))
        pagination = data.get("pagination") if isinstance(data, dict) else None
        has_next = bool(pagination.get("next")) if isinstance(pagination, dict) else False
        return results, has_next

    @_asks_authentik
    def list_applications(self) -> list[str]:
        """App slugs for the add-app picker, via /providers/all/.

        This lists every app that has a provider, regardless of which apps the service account may
        open (the application list would return only those). It needs the service account's role to
        hold the "Can view provider" global permission (authentik_core.view_provider); without it the
        request 403s.
        """
        url = f"{self.base_url}/api/v3/providers/all/"
        slugs: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            params: dict[str, str | int] = {"page_size": 100, "page": page}
            self._log_request("GET", url, params=params)
            response = self.client.get(url, params=params)
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

    @_asks_authentik
    def get_application_by_slug(self, slug: str) -> AuthentikApplication | None:
        """Get application details by exact slug, via the retrieve endpoint.

        The list endpoint ignores ?slug= and, for non-superusers, returns only a per-user cached
        "allowed" list, which can omit apps the service account can access. None only on a 404.
        """
        url = f"{self.base_url}/api/v3/core/applications/{quote(slug, safe='')}/"
        self._log_request("GET", url)
        response = self.client.get(url)
        if response.status_code == 404:
            logger.warning(f"Application '{slug}' not found in Authentik")
            return None
        self._check(response, f"Get application '{slug}'")
        app = self._json(response, f"Get application '{slug}'")
        if not isinstance(app, dict) or not isinstance(app.get("pk"), str):
            raise AuthentikAPIError(f"Get application '{slug}': the answer has no pk")
        logger.info(f"Found application '{slug}' with pk={app['pk']}")
        return cast(AuthentikApplication, app)

    @_asks_authentik
    def get_blueteam_binding(self, app_pk: str) -> AuthentikBinding | None:
        """The application's BlueTeam group binding, or None when it has none."""
        bindings = self._list_all("/api/v3/policies/bindings/", {"target": app_pk}, {"pk": str, "target": str})
        for binding in bindings:
            group_obj = binding.get("group_obj")
            group_name = group_obj.get("name") if isinstance(group_obj, dict) else None
            # Authentik ignores a filter it doesn't know, answering every binding the token may view
            if binding["target"] == app_pk and isinstance(group_name, str) and "blueteam" in group_name.lower():
                logger.info(f"Found blueteam group binding: {binding['pk']} (group={group_name})")
                return cast(AuthentikBinding, binding)
        logger.warning(f"No BlueTeam group binding among {len(bindings)} for application {app_pk}")
        return None

    @_asks_authentik
    def update_binding_enabled(self, binding: AuthentikBinding, enabled: bool) -> None:
        """Set a binding's enabled state; an enabled binding allows the group access."""
        binding_pk = binding["pk"]
        binding["enabled"] = enabled
        response = self.client.put(f"{self.base_url}/api/v3/policies/bindings/{binding_pk}/", json=binding)
        self._check(response, f"Update binding {binding_pk}")
        logger.info(f"Set binding {binding_pk} to {'enabled' if enabled else 'disabled'}")

    def _toggle_application(self, app_slug: str, *, enable: bool) -> tuple[bool, str | None]:
        """Enable or disable an application for blue teams via its BlueTeam group binding."""
        action = "enable" if enable else "disable"
        try:
            app = self.get_application_by_slug(app_slug)
            if not app:
                return False, f"Application '{app_slug}' not found in Authentik"
            binding = self.get_blueteam_binding(app["pk"])
            if not binding:
                return False, f"Application '{app_slug}' has no BlueTeam group binding"
            self.update_binding_enabled(binding, enabled=enable)
        except AuthentikAPIError as e:
            logger.warning(f"Failed to {action} application '{app_slug}': {e}")
            return False, reason_for(e)
        logger.info(f"Application '{app_slug}' {action}d for blue teams")
        return True, None

    def enable_application(self, app_slug: str) -> tuple[bool, str | None]:
        """Enable application for blue teams by enabling the BlueTeam group binding."""
        return self._toggle_application(app_slug, enable=True)

    def disable_application(self, app_slug: str) -> tuple[bool, str | None]:
        """Disable application for blue teams by disabling the BlueTeam group binding."""
        return self._toggle_application(app_slug, enable=False)

    @_asks_authentik
    def update_user_discord_id(self, username: str, discord_id: int, uid: str) -> bool:
        """Store Discord ID in Authentik user attributes, preserving existing attributes; False if it wasn't stored.

        Nothing is written unless the user found by username has this uid (OIDC sub): a stale
        username may since belong to someone else.
        """
        try:
            user = self.find_user(username)
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
            self._check(response, f"Store the Discord ID of {username}")
            logger.info(f"Updated discord_id for Authentik user {username} (pk={user_pk})")
            return True
        except Exception as e:
            logger.exception(f"Failed to update discord_id for user {username}: {e}")
            return False

    @_asks_authentik
    def user_sessions(self, user: AuthentikUser) -> list[uuid.UUID]:
        """The user's Authentik session ids. Raises AuthentikAPIError when Authentik answers sessions of other users,
        an id that isn't a UUID, or anything but a 2xx list."""
        # user__username: Authentik ignores a filter it doesn't know (user=<pk> is one) and answers every session the
        # token may view
        sessions = self._list_all(
            "/api/v3/core/authenticated_sessions/", {"user__username": user["username"]}, {"uuid": str, "user": int}
        )
        if any(session["user"] != user["pk"] for session in sessions):
            raise AuthentikAPIError("Authentik answered sessions of other users")
        try:
            # Canonical: uuid.UUID also accepts {...} and urn:uuid: forms, which the delete path must not carry
            return [uuid.UUID(str(session["uuid"])) for session in sessions]
        except ValueError:
            raise AuthentikAPIError("Authentik answered a session without a valid id") from None

    @_asks_authentik
    def end_sessions(self, session_ids: list[uuid.UUID]) -> str | None:
        """None when every session ended, else why not; never names the user or counts sessions, so one reason
        shared by many teams reads once."""
        refused: int | None = None
        for session_id in session_ids:
            response = self.client.delete(f"{self.base_url}/api/v3/core/authenticated_sessions/{session_id}/")
            # 404: the session ended between the list and the delete
            if not response.is_success and response.status_code != 404 and refused is None:
                refused = response.status_code
        return None if refused is None else f"Authentik did not end sessions (HTTP {refused})"

    @_asks_authentik
    def sees_any_sessions(self) -> bool:
        """Whether the token can see any session at all (when not, see SESSIONS_HIDDEN)."""
        response = self.client.get(f"{self.base_url}/api/v3/core/authenticated_sessions/", params={"page_size": "1"})
        sessions, _ = self._results(response, "List sessions")
        return bool(sessions)

    @_asks_authentik
    def set_user_active(self, user: AuthentikUser, is_active: bool) -> None:
        response = self.client.patch(f"{self.base_url}/api/v3/core/users/{user['pk']}/", json={"is_active": is_active})
        self._check(response, f"Set {user['username']} active={is_active}")

    @_asks_authentik
    def find_user(self, username: str) -> AuthentikUser | None:
        """The user with exactly this username, or None; a user without an integer pk raises."""
        response = self.client.get(f"{self.base_url}/api/v3/core/users/", params={"username": username})
        users, _ = self._results(response, f"Find user {username}")
        user = next((u for u in users if u.get("username") == username), None)
        if user is not None and type(user.get("pk")) is not int:
            raise AuthentikAPIError(f"Find user {username}: the user has no integer pk")
        return cast(AuthentikUser | None, user)

    @_asks_authentik
    def set_password(self, user: AuthentikUser, password: str) -> None:
        response = self.client.post(
            f"{self.base_url}/api/v3/core/users/{user['pk']}/set_password/", json={"password": password}
        )
        self._check(response, f"Set the password of {user['username']}")

    def _list_all(self, path: str, params: dict[str, str], fields: dict[str, type]) -> list[dict[str, object]]:
        """Fetch every page of an Authentik list endpoint, each result holding these fields of these types (a list
        field: of strings). Raises AuthentikAPIError on an answer other than a 2xx list of that shape, or past
        MAX_PAGES."""
        results: list[dict[str, object]] = []
        for page in range(1, MAX_PAGES + 1):
            response = self.client.get(
                f"{self.base_url}{path}", params={**params, "page": str(page), "page_size": "100"}
            )
            found, has_next = self._results(response, f"List {path}")
            for result in found:
                if not all(_holds(result.get(name), kind) for name, kind in fields.items()):
                    raise AuthentikAPIError(f"List {path}: a result without {', '.join(fields)}", url=str(response.url))
            results.extend(found)
            if not has_next:
                return results
        raise AuthentikAPIError(f"List {path}: more than {MAX_PAGES} pages")

    @_asks_authentik
    def list_all_users(self) -> list[AuthentikUserEntry]:
        """Every Authentik user, with ``uid``, ``is_active`` and direct group pks in ``groups``."""
        return cast(
            list[AuthentikUserEntry],
            self._list_all(
                "/api/v3/core/users/", {"include_groups": "false"}, {"uid": str, "is_active": bool, "groups": list}
            ),
        )

    @_asks_authentik
    def list_all_groups(self) -> list[AuthentikGroupEntry]:
        """Every Authentik group, with ``pk``, ``name`` and parent pks in ``parents``."""
        return cast(
            list[AuthentikGroupEntry],
            self._list_all(
                "/api/v3/core/groups/", {"include_users": "false"}, {"pk": str, "name": str, "parents": list}
            ),
        )

    @_asks_authentik
    def get_user_with_groups(self, username: str) -> AuthentikUserWithGroups | None:
        """An Authentik user with their groups_obj, or None if there is none."""
        user = self.find_user(username)
        if user is None:
            return None
        raw = cast(dict[str, object], user)
        groups = raw.get("groups_obj")
        named = isinstance(groups, list) and all(isinstance(g, dict) and isinstance(g.get("name"), str) for g in groups)
        if not named:
            raise AuthentikAPIError(f"Find user {username}: groups without names")
        return cast(AuthentikUserWithGroups, raw)

    @_asks_authentik
    def get_group_by_name(self, name: str) -> AuthentikGroup | None:
        """An Authentik group by exact name, or None if there is none."""
        response = self.client.get(f"{self.base_url}/api/v3/core/groups/", params={"name": name})
        groups, _ = self._results(response, f"Find group {name}")
        found = next((g for g in groups if g.get("name") == name), None)
        if found is not None and not isinstance(found.get("pk"), str):
            raise AuthentikAPIError(f"Find group {name}: the group has no pk")
        return cast(AuthentikGroup | None, found)

    @_asks_authentik
    def add_user_to_group(self, user_pk: int, group_pk: str) -> None:
        response = self.client.post(f"{self.base_url}/api/v3/core/groups/{group_pk}/add_user/", json={"pk": user_pk})
        self._check(response, f"Add user {user_pk} to group {group_pk}")
