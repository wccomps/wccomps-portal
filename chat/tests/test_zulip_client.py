"""ZulipClient: each request's path, auth and fields, the answer it parses, and Zulip's refusals as ZulipError."""

import base64
import json
from collections.abc import AsyncIterator, Callable
from urllib.parse import parse_qs

import httpx
import pytest

from chat.zulip.client import LONG_POLL_READ_TIMEOUT, ZulipClient, ZulipError

AUTH = "Basic " + base64.b64encode(b"portal-bot@zulip.example.org:test-key").decode()


class Recorder:
    """A MockTransport that answers every request with one JSON answer and keeps the requests."""

    def __init__(self, answer: object, status: int = 200) -> None:
        self.answer = answer
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.answer)

    @property
    def request(self) -> httpx.Request:
        (request,) = self.requests
        return request

    def form(self) -> dict[str, str]:
        return {key: value for key, (value,) in parse_qs(self.request.content.decode()).items()}


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> ZulipClient:
    return ZulipClient(
        "https://zulip.example.org/", "portal-bot@zulip.example.org", "test-key", transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize("site", ["http://zulip.example.org", "zulip.example.org", ""])
def test_a_site_that_isnt_https_is_refused(site):
    with pytest.raises(ValueError, match="https"):
        ZulipClient(site, "portal-bot@zulip.example.org", "test-key")


async def test_send_stream_message_posts_to_the_stream_topic_and_answers_the_id():
    recorder = Recorder({"result": "success", "msg": "", "id": 42})

    message_id = await client_for(recorder).send_stream_message("team03", "T003-001 Web server down", "**hi**")

    assert message_id == 42
    assert recorder.request.method == "POST"
    assert recorder.request.url == "https://zulip.example.org/api/v1/messages"
    assert recorder.request.headers["authorization"] == AUTH
    assert recorder.form() == {
        "type": "stream",
        "to": "team03",
        "topic": "T003-001 Web server down",
        "content": "**hi**",
    }


async def test_add_reaction_posts_the_emoji_name():
    recorder = Recorder({"result": "success", "msg": ""})

    assert await client_for(recorder).add_reaction(77, "paperclip") is None

    assert recorder.request.method == "POST"
    assert recorder.request.url == "https://zulip.example.org/api/v1/messages/77/reactions"
    assert recorder.request.headers["authorization"] == AUTH
    assert recorder.form() == {"emoji_name": "paperclip"}


async def test_register_queue_asks_for_messages_edits_and_deletes_in_raw_markdown():
    recorder = Recorder({"result": "success", "msg": "", "queue_id": "1517975029:0", "last_event_id": -1})

    assert await client_for(recorder).register_queue() == ("1517975029:0", -1)

    assert recorder.request.method == "POST"
    assert recorder.request.url == "https://zulip.example.org/api/v1/register"
    assert recorder.request.headers["authorization"] == AUTH
    form = recorder.form()
    assert json.loads(form["event_types"]) == ["message", "update_message", "delete_message"]
    assert form["apply_markdown"] == "false"


async def test_get_events_long_polls_the_queue_and_answers_its_events():
    events = [{"type": "message", "id": 3, "message": {"id": 77}}, {"type": "heartbeat", "id": 4}]
    recorder = Recorder({"result": "success", "msg": "", "events": events})

    assert await client_for(recorder).get_events("1517975029:0", 2) == events

    request = recorder.request
    assert request.method == "GET"
    assert request.url.path == "/api/v1/events"
    assert dict(request.url.params) == {"queue_id": "1517975029:0", "last_event_id": "2"}
    assert request.headers["authorization"] == AUTH
    assert request.extensions["timeout"]["read"] == LONG_POLL_READ_TIMEOUT


async def test_get_user_email_answers_the_users_real_address():
    user = {"user_id": 9, "email": "user9@zulip.example.org", "delivery_email": "member@example.org"}
    recorder = Recorder({"result": "success", "msg": "", "user": user})

    assert await client_for(recorder).get_user_email(9) == "member@example.org"

    request = recorder.request
    assert request.method == "GET"
    assert request.url.path == "/api/v1/users/9"
    assert request.headers["authorization"] == AUTH


@pytest.mark.parametrize(
    "user",
    [
        {"user_id": 9, "email": "member@example.org"},
        {"user_id": 9, "email": "user9@zulip.example.org", "delivery_email": None},
    ],
    ids=["before_zulip_7_public", "hidden_from_the_bot"],
)
async def test_get_user_email_falls_back_to_email_without_a_delivery_email(user):
    """Before Zulip 7.0 a public address has no delivery_email; one hidden from the bot has it null, and email is
    Zulip's placeholder, which matches no portal account."""
    recorder = Recorder({"result": "success", "msg": "", "user": user})

    assert await client_for(recorder).get_user_email(9) == user["email"]


@pytest.mark.parametrize("answer", [{}, {"user": None}, {"user": {"user_id": 9}}, {"user": {"email": ""}}])
async def test_get_user_email_without_an_email_raises(answer):
    with pytest.raises(ZulipError) as error:
        await client_for(Recorder({"result": "success", "msg": "", **answer})).get_user_email(9)

    assert error.value.code == "BAD_ANSWER"


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.send_stream_message("team03", "topic", "hi"),
        lambda c: c.add_reaction(77, "paperclip"),
        lambda c: c.register_queue(),
        lambda c: c.get_events("1517975029:0", 2),
        lambda c: c.get_user_email(9),
    ],
    ids=["send_stream_message", "add_reaction", "register_queue", "get_events", "get_user_email"],
)
async def test_an_error_answer_raises_zulip_error_with_its_code(call):
    recorder = Recorder({"result": "error", "code": "BAD_EVENT_QUEUE_ID", "msg": "Bad event queue ID"}, status=400)

    with pytest.raises(ZulipError) as error:
        await call(client_for(recorder))

    assert error.value.code == "BAD_EVENT_QUEUE_ID"
    assert str(error.value) == "BAD_EVENT_QUEUE_ID: Bad event queue ID"


async def test_an_answer_that_is_not_json_raises_with_its_status():
    client = client_for(lambda request: httpx.Response(502, content=b"<html>502 Bad Gateway</html>"))

    with pytest.raises(ZulipError) as error:
        await client.send_stream_message("team03", "topic", "hi")

    assert error.value.code == "HTTP_502"


async def test_an_answer_without_the_message_id_raises():
    with pytest.raises(ZulipError) as error:
        await client_for(Recorder({"result": "success", "msg": ""})).send_stream_message("team03", "topic", "hi")

    assert error.value.code == "BAD_ANSWER"


class Chunks(httpx.AsyncByteStream):
    """A body served in chunks, counting how many were read."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read += 1
            yield chunk


async def test_fetch_upload_downloads_the_file_with_the_bot_credentials():
    recorder: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, stream=Chunks([b"abc", b"def"]))

    assert await client_for(handler).fetch_upload("/user_uploads/2/ab/notes.txt", limit=7) == b"abcdef"

    (request,) = recorder
    assert request.method == "GET"
    assert request.url == "https://zulip.example.org/user_uploads/2/ab/notes.txt"
    assert request.headers["authorization"] == AUTH


async def test_fetch_upload_reads_no_further_than_the_limit():
    body = Chunks([b"a" * 4, b"b" * 4, b"c" * 4, b"d" * 4])
    client = client_for(lambda request: httpx.Response(200, stream=body))

    assert await client.fetch_upload("/user_uploads/2/ab/big.bin", limit=7) == b"aaaabbb"

    assert body.read == 2


async def test_fetch_upload_raises_on_an_error_status():
    client = client_for(lambda request: httpx.Response(404, content=b"not found"))

    with pytest.raises(ZulipError) as error:
        await client.fetch_upload("/user_uploads/2/ab/gone.txt", limit=7)

    assert error.value.code == "HTTP_404"


@pytest.mark.parametrize(
    "path",
    [
        "https://elsewhere.example.org/user_uploads/x",
        "@elsewhere.example.org/user_uploads/x",
        "/user_uploads/../api/v1/users",
        "/api/v1/users",
        # Encoded or backslashed climbs a proxy or server could decode or normalize into another path
        "/user_uploads/%2e%2e/api/v1/messages/1",
        "/user_uploads/2/%2E%2E/%2e%2e/api/v1/users",
        "/user_uploads/2/ab/..%2f..%2fapi/v1/users",
        "/user_uploads/2/ab\\..\\..\\api/v1/users",
        "/user_uploads/2/./ab/x.png",
        "/user_uploads/2/ab/../../api",
        "/user_uploads/x/ab/notes.txt",
        "/user_uploads/2/",
        "/user_uploads/2/ab/notes.txt\n",
    ],
)
async def test_fetch_upload_sends_credentials_only_to_the_sites_uploads(path):
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"requested {request.url}")

    with pytest.raises(ZulipError) as error:
        await client_for(handler).fetch_upload(path, limit=7)

    assert error.value.code == "NOT_AN_UPLOAD"
