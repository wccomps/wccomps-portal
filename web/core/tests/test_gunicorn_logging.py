"""gunicorn's access and error logs, as gunicorn.conf.py sets them up: request text can't forge a line or a field."""

import datetime
import io
import runpy
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from gunicorn.config import Config
from gunicorn.glogging import Logger

from core.tests.test_properties_log_lines import _unescaped_quotes

WEB = Path(__file__).resolve().parents[2]


@pytest.fixture
def gunicorn_logs() -> Iterator[tuple[Logger, dict[str, io.StringIO]]]:
    config = Config()
    for name, value in runpy.run_path(str(WEB / "gunicorn.conf.py")).items():
        if name in config.settings:
            config.set(name, value)
    logger = config.logger_class(config)
    logs = {"access": logger.access_log, "error": logger.error_log}
    streams = {}
    for name, log in logs.items():
        for handler in log.handlers:
            if getattr(handler, "_gunicorn", False):
                streams[name] = io.StringIO()
                handler.setStream(streams[name])
    yield logger, streams
    for log in logs.values():
        for handler in [h for h in log.handlers if getattr(h, "_gunicorn", False)]:
            log.removeHandler(handler)


def test_an_access_line_keeps_request_text_inside_its_fields(gunicorn_logs) -> None:
    logger, streams = gunicorn_logs
    environ = {
        "REMOTE_ADDR": "10.0.0.1",
        "REQUEST_METHOD": "GET",
        "RAW_URI": "/x",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "PATH_INFO": '/x"\n10.0.0.2 - - "GET /ops/ HTTP/1.1" 200',
        "HTTP_USER_AGENT": 'agent"\r\nforged',
    }
    response = SimpleNamespace(status="200 OK", sent=0, headers=[])
    logger.access(response, SimpleNamespace(headers=[]), environ, datetime.timedelta(seconds=0))

    line = streams["access"].getvalue()
    assert len(line.splitlines()) == 1, line
    assert _unescaped_quotes(line) == 4, line
    assert '/x\\"\\n10.0.0.2' in line
    assert '\\\\"' not in line
    assert "%(u)s" not in logger.cfg.access_log_format


def test_an_error_line_keeps_a_raw_request_uri_on_one_line(gunicorn_logs) -> None:
    logger, streams = gunicorn_logs
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("Error handling request %s", "/x\n[ERROR] forged")

    lines = streams["error"].getvalue().splitlines()
    assert "/x\\n[ERROR] forged" in lines[0]
    assert lines[1] == "Traceback (most recent call last):"
