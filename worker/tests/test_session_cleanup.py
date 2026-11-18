"""The worker deletes expired sessions: every anonymous visit to the sign-in page adds one, and nothing else ever
deletes them."""

from datetime import timedelta

import pytest
from django.contrib.sessions.models import Session
from django.utils import timezone

from worker import jobs
from worker.tests.loops import one_pass

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def hb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_HEARTBEAT_DIR", str(tmp_path))
    return tmp_path


async def test_a_pass_deletes_expired_sessions_keeps_live_ones_and_beats(hb_dir, monkeypatch):
    now = timezone.now()
    await Session.objects.acreate(session_key="expired", session_data="", expire_date=now - timedelta(minutes=1))
    await Session.objects.acreate(session_key="live", session_data="", expire_date=now + timedelta(hours=1))

    await one_pass(jobs.session_cleanup_loop, monkeypatch)

    assert [s.session_key async for s in Session.objects.all()] == ["live"]
    assert (hb_dir / "sessions").exists()


async def test_a_failed_pass_skips_the_beat(hb_dir, monkeypatch):
    def broken(*_args: object) -> None:
        raise RuntimeError("database gone")

    monkeypatch.setattr(jobs, "call_command", broken)

    await one_pass(jobs.session_cleanup_loop, monkeypatch)

    assert not (hb_dir / "sessions").exists()
