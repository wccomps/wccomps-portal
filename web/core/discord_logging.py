"""Discord webhook logging handler for error alerts."""

import logging
import os
import traceback
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


def _code(text: str) -> str:
    """text as one inline code span, inside which Discord renders no markdown, link or mention."""
    inner = escape_unprintable(text).replace("`", _BACKTICK) or " "
    return f"`{inner[: FIELD_LIMIT - 2]}`"


class DiscordWebhookHandler(logging.Handler):
    """Logging handler that sends error messages to the DISCORD_ERROR_WEBHOOK_URL Discord webhook."""

    def __init__(self) -> None:
        super().__init__()
        webhook_url = os.environ.get("DISCORD_ERROR_WEBHOOK_URL", "")
        self.webhook_url = webhook_url if webhook_url.startswith("https://") else ""
        self.hostname = os.environ.get("HOSTNAME", "unknown")

    def emit(self, record: logging.LogRecord) -> None:
        if not self.webhook_url:
            return

        try:
            timestamp = datetime.now(UTC).isoformat()

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
                "timestamp": timestamp,
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
                fields.insert(
                    0, {"name": "Request", "value": _code(f"{request.method} {request.path}"), "inline": True}
                )
                if hasattr(request, "user") and request.user.is_authenticated:
                    fields.insert(1, {"name": "User", "value": _code(request.user.username), "inline": True})

            payload = {"embeds": [embed]}

            # Send to Discord (sync, but logging handlers should be fast)
            response = httpx.post(self.webhook_url, json=payload, timeout=settings.DISCORD_WEBHOOK_TIMEOUT)
            if response.is_error:
                # This module's logger reaches the console only, never this handler
                logger.warning("Discord refused an error alert: HTTP %s %s", response.status_code, response.text[:200])

        except Exception:
            # Don't let logging errors break the app
            self.handleError(record)
