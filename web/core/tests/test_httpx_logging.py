"""httpx request URLs must not be logged: they carry the Discord webhook token and OAuth codes.

This checks settings.LOGGING, which both the web app and the bot apply (the bot runs django.setup()).
"""

import logging


def test_httpx_logger_suppresses_request_urls():
    # httpx logs "HTTP Request: <method> <url> ..." at INFO; the URL carries secrets, so it must be WARNING+.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
