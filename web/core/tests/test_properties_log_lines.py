"""Request text can't forge a log line or a field: the access and error lines under any path and header."""

import logging

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from core.tests.views_harness import hostile_text

LINE_BREAKS = st.sampled_from(["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029"])
DISGUISES = st.sampled_from(["\u202e", "\u2066", "\u2069", "\u200b", "\u00ad"])
FORGED = st.lists(
    st.one_of(hostile_text(64), LINE_BREAKS, DISGUISES, st.sampled_from(['"', "\\", "\\n", " ", "\xa0"])), max_size=6
).map("".join)


def _records(run) -> list[logging.LogRecord]:
    """The records wccomps.access and wccomps.errors write while run() runs."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    loggers = [logging.getLogger("wccomps.access"), logging.getLogger("wccomps.errors")]
    # The view property modules switch logging off (views_harness.offline) for as long as their worlds last
    disabled = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for log in loggers:
        log.addHandler(handler)
    try:
        run()
    except RuntimeError:
        pass
    finally:
        for log in loggers:
            log.removeHandler(handler)
        logging.disable(disabled)
    assert records, "nothing was logged"
    return records


def _logged(run) -> list[str]:
    return [record.getMessage() for record in _records(run)]


def _unescaped_quotes(message: str) -> int:
    count, escaped = 0, False
    for char in message:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            count += 1
    return count


def _fields(message: str) -> int:
    """How many fields a reader splitting on unescaped whitespace outside quotes finds."""
    count, escaped, quoted, in_field = 0, False, False, False
    for char in message:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char.isspace() and not quoted:
            in_field = False
            continue
        if not in_field:
            count, in_field = count + 1, True
    return count


def _assert_one_line_one_quoted_field(messages: list[str], fields: int) -> None:
    assert messages
    for message in messages:
        assert len(message.splitlines()) == 1, message
        assert message.isprintable(), message
        assert _unescaped_quotes(message) == 2, message
        assert _fields(message) == fields, message


def _access_logged(path: str = "/", *, host: str | None = None, raises: bool = False, status: int = 200) -> list[str]:
    """The lines an anonymous GET of this path (with this Host header, if any) logs, its view answering this status
    or raising."""
    from django.contrib.auth.models import AnonymousUser
    from django.http import HttpResponse
    from django.test import RequestFactory

    from core.middleware import AccessLoggingMiddleware

    def view(request):
        if raises:
            raise RuntimeError("boom")
        return HttpResponse(status=status)

    request = RequestFactory().get("/")
    request.path = path
    if host is not None:
        request.META["HTTP_HOST"] = host
    request.user = AnonymousUser()
    return _logged(lambda: AccessLoggingMiddleware(view)(request))


@given(path=FORGED, host=FORGED, raises=st.booleans(), status=st.sampled_from([200, 500]))
@example(path='x" 200 1ms [0q 0ms]\nINFO "GET /ops/', host="h", raises=False, status=200)
@example(path="x", host="h - forged", raises=False, status=200)
def test_an_access_log_line_is_one_line_whatever_the_path_and_host(
    path: str, host: str, raises: bool, status: int
) -> None:
    """A line break or quote in a path or Host header would otherwise forge a field or a whole line."""
    # client_ip user host "method path" status duration [queries time], or (unhandled exception) at the end
    _assert_one_line_one_quoted_field(_access_logged("/" + path, host=host, raises=raises, status=status), fields=8)


def test_a_logged_newline_and_a_literal_backslash_n_stay_distinct() -> None:
    (newline,) = _access_logged("/a\nb")
    (backslash_n,) = _access_logged("/a\\nb")
    assert '"GET /a\\nb"' in newline
    assert '"GET /a\\\\nb"' in backslash_n


@pytest.mark.parametrize("raises", [False, True])
def test_a_logged_path_keeps_its_spaces_inside_its_quotes(raises: bool) -> None:
    """The path is the one quoted field, so a space in it is written as is (an unquoted field escapes it)."""
    messages = _access_logged("/a b/", raises=raises)

    assert all('"GET /a b/"' in message for message in messages), messages


def test_a_request_without_a_host_header_logs_a_dash_for_it() -> None:
    (message,) = _access_logged()

    # client_ip user host "method path" ...
    assert message.split()[1:4] == ["-", "-", '"GET']


def test_an_unhandled_exception_keeps_its_traceback() -> None:
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    from core.middleware import AccessLoggingMiddleware

    def view(request):
        raise RuntimeError("boom")

    request = RequestFactory().get("/")
    request.path = '/a\n"b'
    request.user = AnonymousUser()
    (record,) = [r for r in _records(lambda: AccessLoggingMiddleware(view)(request)) if r.name == "wccomps.errors"]

    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert '"GET /a\\n\\"b"' in record.getMessage()
