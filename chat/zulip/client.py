"""An async client for the parts of Zulip's REST API the portal uses: sending to a stream, reacting, the event queue
and downloading an upload. The bot authenticates with its email and API key (HTTP basic auth)."""

import json

import httpx
from django.conf import settings

# Zulip answers a long-poll within about a minute (a heartbeat event when nothing happened), so this outlasts it
LONG_POLL_READ_TIMEOUT = 120.0
EVENT_TYPES = ["message", "update_message", "delete_message"]
UPLOAD_PREFIX = "/user_uploads/"


class ZulipError(Exception):
    """Zulip refused a request. code is Zulip's error code ("BAD_EVENT_QUEUE_ID", "STREAM_DOES_NOT_EXIST", ...),
    "HTTP_<status>" for an answer that isn't Zulip's JSON, or "BAD_ANSWER" for JSON without what was asked for."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class ZulipClient:
    def __init__(self, site: str, email: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not site.lower().startswith("https://"):
            raise ValueError(f"ZULIP_SITE must be an https:// URL, not {site!r}: every request carries the API key")
        self._http = httpx.AsyncClient(
            base_url=site.rstrip("/"),
            auth=httpx.BasicAuth(email, api_key),
            timeout=httpx.Timeout(settings.HTTPX_DEFAULT_TIMEOUT),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, endpoint: str, *, long_poll: bool = False, **fields: str) -> dict[str, object]:
        """Zulip's JSON answer to a request (form fields for a POST, query parameters for a GET), when it succeeded."""
        params, data = (fields, None) if method == "GET" else (None, fields)
        response = await self._http.request(
            method,
            f"/api/v1/{endpoint}",
            params=params,
            data=data,
            timeout=(
                httpx.Timeout(settings.HTTPX_DEFAULT_TIMEOUT, read=LONG_POLL_READ_TIMEOUT)
                if long_poll
                else httpx.USE_CLIENT_DEFAULT
            ),
        )
        try:
            answer = response.json()
        except ValueError, RecursionError:
            answer = None
        if not isinstance(answer, dict):
            raise ZulipError(f"HTTP_{response.status_code}")
        code, message = answer.get("code"), answer.get("msg")
        message = message if isinstance(message, str) else ""
        if answer.get("result") != "success":
            raise ZulipError(code if isinstance(code, str) and code else f"HTTP_{response.status_code}", message)
        if not response.is_success:
            raise ZulipError(f"HTTP_{response.status_code}", message)
        return answer

    async def send_stream_message(self, stream: str, topic: str, content: str) -> int:
        """Post to a stream's topic; the new message's id."""
        answer = await self._call("POST", "messages", type="stream", to=stream, topic=topic, content=content)
        message_id = answer.get("id")
        if type(message_id) is not int:
            raise ZulipError("BAD_ANSWER", "no message id")
        return message_id

    async def add_reaction(self, message_id: int, emoji_name: str) -> None:
        await self._call("POST", f"messages/{message_id}/reactions", emoji_name=emoji_name)

    async def register_queue(self) -> tuple[str, int]:
        """A new event queue for messages and their edits and deletes, with raw Markdown: (queue_id, last_event_id).
        Zulip puts on it only messages in the streams the bot is subscribed to."""
        answer = await self._call("POST", "register", event_types=json.dumps(EVENT_TYPES), apply_markdown="false")
        queue_id, last_event_id = answer.get("queue_id"), answer.get("last_event_id")
        if not isinstance(queue_id, str) or not queue_id or type(last_event_id) is not int:
            raise ZulipError("BAD_ANSWER", "no queue id or last event id")
        return queue_id, last_event_id

    async def get_events(self, queue_id: str, last_event_id: int) -> list[dict[str, object]]:
        """The queue's events after last_event_id, waiting for one (a long poll)."""
        answer = await self._call("GET", "events", long_poll=True, queue_id=queue_id, last_event_id=str(last_event_id))
        events = answer.get("events")
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            raise ZulipError("BAD_ANSWER", "no list of events")
        return events

    async def fetch_upload(self, path: str, limit: int) -> bytes:
        """An uploaded file's first limit bytes (path as a message links it, /user_uploads/...), read no further: ask
        for one byte past a size cap to tell a file over it. Only the site's own uploads: the request carries the
        bot's credentials."""
        if not path.startswith(UPLOAD_PREFIX) or ".." in path.split("/"):
            raise ZulipError("NOT_AN_UPLOAD", path)
        async with self._http.stream("GET", path) as response:
            if not response.is_success:
                raise ZulipError(f"HTTP_{response.status_code}")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) >= limit:
                    break
            return bytes(data[:limit])
