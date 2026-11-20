"""Discord webhook logging handler for error alerts.

An error a visitor can cause on purpose must cost a request nothing and the alert channel little: each alert is
posted from a background thread, a repeat of the same error within REPEAT_WINDOW is held back, as is any alert past
PER_MINUTE in a minute, and the next alert sent says how many were held back.
"""

import logging
import os
import queue
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from django.conf import settings

from core.log_safety import escape_unprintable

logger = logging.getLogger(__name__)

# Discord's embed limits: a longer title or field value makes it refuse the whole alert
TITLE_LIMIT = 256
FIELD_LIMIT = 1024
# A backtick lookalike: request text then holds no backtick to end its code span or the traceback's block
_BACKTICK = "\u02cb"

REPEAT_WINDOW = 600  # seconds an error's repeats are held back after it is sent
PER_MINUTE = 5  # alerts sent in any 60 seconds
QUEUE_SIZE = 20  # alerts waiting for the sender; more are held back

type Signature = tuple[str, int, str, str, str]
type Payload = dict[str, object]


def _code(text: str) -> str:
    """text as one inline code span, inside which Discord renders no markdown, link or mention."""
    inner = escape_unprintable(text).replace("`", _BACKTICK) or " "
    return f"`{inner[: FIELD_LIMIT - 2]}`"


def signature(record: logging.LogRecord) -> Signature:
    """What makes two records the same error: the logger, level and message template (not its arguments, which
    hold the request's path), and the exception's type and the line it was raised at."""
    kind = where = ""
    if record.exc_info and record.exc_info[1] is not None:
        error = record.exc_info[1]
        kind = type(error).__qualname__
        if frames := traceback.extract_tb(error.__traceback__):
            where = f"{frames[-1].filename}:{frames[-1].lineno}"
    return (record.name, record.levelno, str(record.msg), kind, where)


class DiscordWebhookHandler(logging.Handler):
    """Logging handler that sends error messages to the DISCORD_ERROR_WEBHOOK_URL Discord webhook."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        webhook_url = os.environ.get("DISCORD_ERROR_WEBHOOK_URL", "")
        self.webhook_url = webhook_url if webhook_url.startswith("https://") else ""
        self.hostname = os.environ.get("HOSTNAME", "unknown")
        self.clock = clock
        self._start()

    def _start(self) -> None:
        """Fresh state and no sender yet, in this process (a forked worker inherits neither thread nor locks)."""
        self._pid = os.getpid()
        self._gate = threading.Lock()
        self._sent: dict[Signature, float] = {}
        self._recent: deque[float] = deque()
        self._held_back = 0
        self.outbox: queue.Queue[Payload] = queue.Queue(QUEUE_SIZE)
        self._sender: threading.Thread | None = None

    def emit(self, record: logging.LogRecord) -> None:
        if not self.webhook_url:
            return
        try:
            if self._pid != os.getpid():
                self._start()
            held_back = self._admit(signature(record))
            if held_back is None:
                return
            try:
                self.outbox.put_nowait(self.payload(record, held_back))
            except queue.Full:
                with self._gate:
                    self._held_back += 1 + held_back
                return
            self._ensure_sender()
        except Exception:
            # Don't let logging errors break the app
            self.handleError(record)

    def _admit(self, key: Signature) -> int | None:
        """None to hold the alert back; else how many were held back before it, which it then reports."""
        now = self.clock()
        with self._gate:
            for old in [k for k, sent in self._sent.items() if now - sent >= REPEAT_WINDOW]:
                del self._sent[old]
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if key in self._sent or len(self._recent) >= PER_MINUTE:
                self._held_back += 1
                return None
            self._sent[key] = now
            self._recent.append(now)
            held_back, self._held_back = self._held_back, 0
            return held_back

    def _ensure_sender(self) -> None:
        with self._gate:
            if self._sender is None or not self._sender.is_alive():
                self._sender = threading.Thread(target=self._send_forever, name="discord-error-alerts", daemon=True)
                self._sender.start()

    def _send_forever(self) -> None:
        while True:
            payload = self.outbox.get()
            try:
                self.send(payload)
            except Exception:
                # This module's logger reaches the console only, never this handler
                logger.warning("Could not send an error alert to Discord", exc_info=True)
            finally:
                self.outbox.task_done()

    def send(self, payload: Payload) -> None:
        response = httpx.post(self.webhook_url, json=payload, timeout=settings.DISCORD_WEBHOOK_TIMEOUT)
        if response.is_error:
            logger.warning("Discord refused an error alert: HTTP %s %s", response.status_code, response.text[:200])

    def payload(self, record: logging.LogRecord, held_back: int = 0) -> Payload:
        """The webhook body for record, built in the logging thread (it reads the request)."""
        exc_info = ""
        if record.exc_info:
            exc_info = "".join(traceback.format_exception(*record.exc_info))

        fields: list[dict[str, object]] = [
            {"name": "Message", "value": _code(record.getMessage()), "inline": False},
            {"name": "Logger", "value": record.name, "inline": True},
            {"name": "Host", "value": self.hostname, "inline": True},
        ]
        embed: dict[str, object] = {
            "title": f"{record.levelname} from {record.name}"[:TITLE_LIMIT],
            "color": 0xFF0000 if record.levelname == "ERROR" else 0xFFA500,
            "timestamp": datetime.now(UTC).isoformat(),
            "fields": fields,
        }

        if exc_info:
            # The tail, where the exception is, within the field limit with its fences
            shown = "\n".join(escape_unprintable(line) for line in exc_info.split("\n"))
            tail = shown.replace("`", _BACKTICK)[-(FIELD_LIMIT - 8) :]
            fields.append({"name": "Traceback", "value": f"```\n{tail}\n```", "inline": False})

        # Add request path if available (from django.request logger)
        if hasattr(record, "request"):
            request = record.request
            fields.insert(0, {"name": "Request", "value": _code(f"{request.method} {request.path}"), "inline": True})
            if hasattr(request, "user") and request.user.is_authenticated:
                fields.insert(1, {"name": "User", "value": _code(request.user.username), "inline": True})

        if held_back:
            fields.append(
                {
                    "name": "Held back",
                    "value": f"{held_back} alerts since the last one: repeats within {REPEAT_WINDOW // 60} minutes, "
                    f"or over {PER_MINUTE} a minute. The logs have them all.",
                    "inline": False,
                }
            )

        return {"embeds": [embed]}
