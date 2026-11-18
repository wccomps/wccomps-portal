"""The scoring-host archive script: it takes the right event's divisor log and every submission's contents, never
replaces a file in the archive directory, and its check catches a copy that no longer matches what was archived."""

import io
import json
import os
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from quotient import archive_scoring_host
from quotient.archive_scoring_host import ArchiveError, archive, main, verify

LOG = 'time=2026-10-31T18:24:18.305Z level=DEBUG msg="Added SNAT rule" ip=192.0.2.5\n'


@pytest.fixture
def docker(tmp_path, monkeypatch):
    """A docker on PATH that lists two divisor containers, this event's (started from FAKE_EVENT_DIR) and another
    event's, and prints LOG (plus a stderr line) as the first one's logs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "docker"
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'ps':\n"
        "    print(f\"c0ffee {os.environ['FAKE_EVENT_DIR']}\")\n"
        "    print('dead00 /elsewhere/2027-inv8-other')\n"
        "elif args[:2] == ['logs', 'c0ffee']:\n"
        f"    sys.stdout.write({LOG!r}); sys.stdout.flush(); sys.stderr.write('stderr line\\n')\n"
        "else:\n"
        "    sys.exit('no such container')\n"
    )
    path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return path


@pytest.fixture
def event_dir(tmp_path, monkeypatch):
    event = tmp_path / "events" / "2027-inv9-example"
    (event / "submissions" / "2" / "38").mkdir(parents=True)
    (event / "submissions" / "2" / "38" / "1").write_bytes(b"inject 02, team 38, attempt 1")
    monkeypatch.setenv("FAKE_EVENT_DIR", str(event))
    return event


@pytest.fixture
def archive_dir(tmp_path):
    path = tmp_path / "archive"
    path.mkdir()
    return path


def _members(target: Path) -> dict[str, bytes]:
    with tarfile.open(target, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def test_archive_holds_the_events_submissions_and_divisor_log(event_dir, archive_dir, docker):
    target, digest, links = archive(event_dir, archive_dir)
    assert links == []

    assert sorted(p.name for p in archive_dir.iterdir()) == [target.name, f"{target.name}.sha256"]
    assert target.name.startswith("2027-inv9-example-scoring-host-")
    assert target.with_name(f"{target.name}.sha256").read_text().split() == [digest, target.name]
    # Team uploads and runner addresses: readable only by whoever ran the archive
    assert {p.stat().st_mode & 0o777 for p in archive_dir.iterdir()} == {0o600}
    members = _members(target)
    manifest = json.loads(members.pop("manifest.json"))
    assert members == {
        "divisor.log": (LOG + "stderr line\n").encode(),
        "submissions/2/38/1": b"inject 02, team 38, attempt 1",
    }
    assert (manifest["event"], manifest["divisor_container"]) == ("2027-inv9-example", "c0ffee")
    assert set(manifest["files"]) == set(members)
    assert verify(target) == []


def test_links_inside_submissions_are_left_out_and_named(tmp_path, archive_dir, docker, monkeypatch):
    """A submissions directory kept on another disk behind a symlink, an event directory reached through one, and a
    hard-linked upload are archived; a symlink below submissions/ is not followed (it could name any file on the
    host) but is listed in the manifest."""
    real_event = tmp_path / "disk" / "2027-inv9-example"
    elsewhere = tmp_path / "disk" / "uploads"
    (elsewhere / "2" / "38").mkdir(parents=True)
    (elsewhere / "2" / "38" / "1").write_bytes(b"attempt 1")
    os.link(elsewhere / "2" / "38" / "1", elsewhere / "2" / "38" / "2")
    secret = tmp_path / "secret"
    secret.write_bytes(b"host file")
    (elsewhere / "2" / "38" / "3").symlink_to(secret)
    (elsewhere / "2" / "linked").symlink_to(tmp_path)
    real_event.mkdir()
    (real_event / "submissions").symlink_to(elsewhere)
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "2027-inv9-example").symlink_to(real_event)
    monkeypatch.setenv("FAKE_EVENT_DIR", str(tmp_path / "events" / "2027-inv9-example"))

    target, _, links = archive(real_event, archive_dir)

    members = _members(target)
    manifest = json.loads(members.pop("manifest.json"))
    assert set(members) == {"divisor.log", "submissions/2/38/1", "submissions/2/38/2"}
    assert (members["submissions/2/38/1"], members["submissions/2/38/2"]) == (b"attempt 1", b"attempt 1")
    assert links == manifest["links_left_out"] == ["2/38/3", "2/linked"]
    assert verify(target) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_an_unreadable_directory_stops_the_archive(event_dir, archive_dir, docker):
    locked = event_dir / "submissions" / "3"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(ArchiveError, match="Could not read"):
            archive(event_dir, archive_dir)
    finally:
        locked.chmod(0o700)
    assert list(archive_dir.iterdir()) == []


def test_another_events_divisor_is_not_taken(event_dir, archive_dir, docker, monkeypatch):
    monkeypatch.setenv("FAKE_EVENT_DIR", "/elsewhere/2027-inv7-older")

    with pytest.raises(ArchiveError, match="No divisor container was started from"):
        archive(event_dir, archive_dir)
    assert list(archive_dir.iterdir()) == []


def test_a_failed_log_read_leaves_nothing(event_dir, archive_dir, docker):
    docker.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "print('c0ffee', os.environ['FAKE_EVENT_DIR']) if sys.argv[1] == 'ps' else sys.exit(3)\n"
    )

    with pytest.raises(ArchiveError, match="docker logs c0ffee failed"):
        archive(event_dir, archive_dir)
    assert list(archive_dir.iterdir()) == []


def test_an_existing_file_is_never_replaced(event_dir, archive_dir, docker, monkeypatch):
    fixed = datetime(2027, 2, 6, 23, 0, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(archive_scoring_host, "datetime", Frozen)
    first, _, _ = archive(event_dir, archive_dir)
    kept = first.read_bytes()

    with pytest.raises(ArchiveError, match="already exists"):
        archive(event_dir, archive_dir)
    assert first.read_bytes() == kept
    assert sorted(p.name for p in archive_dir.iterdir()) == [first.name, f"{first.name}.sha256"]


def test_verify_catches_a_changed_or_damaged_copy(event_dir, archive_dir, docker):
    target, _, _ = archive(event_dir, archive_dir)
    checksum = target.with_name(f"{target.name}.sha256")

    # A file swapped inside a rebuilt tarball, with a matching .sha256: only the manifest can tell
    rebuilt = io.BytesIO()
    with tarfile.open(target, "r:gz") as old, tarfile.open(fileobj=rebuilt, mode="w:gz") as new:
        for member in old.getmembers():
            data = old.extractfile(member).read()
            if member.name == "divisor.log":
                data = b"edited\n"
                member.size = len(data)
            new.addfile(member, io.BytesIO(data))
    target.write_bytes(rebuilt.getvalue())
    checksum.write_text(f"{archive_scoring_host.sha256_of(target)}  {target.name}\n")
    assert verify(target) == ["divisor.log does not match its manifest entry"]

    target.write_bytes(b"truncated")
    assert verify(target) == [f"{target.name} does not match {checksum.name}"]
    checksum.write_text(f"{archive_scoring_host.sha256_of(target)}  {target.name}\n")
    assert verify(target)[0].startswith(f"Could not read {target.name}")
    checksum.write_text("")
    assert verify(target) == [f"{checksum.name} is empty"]


def test_the_command_reports_the_tarball_or_why_not(event_dir, archive_dir, docker, capsys, monkeypatch):
    assert main(["archive", str(event_dir), str(archive_dir)]) == 0
    tarball, digest = capsys.readouterr().out.split()
    assert Path(tarball).parent == archive_dir
    assert main(["verify", tarball]) == 0

    monkeypatch.setenv("FAKE_EVENT_DIR", "/elsewhere")
    assert main(["archive", str(event_dir), str(archive_dir)]) == 1
    assert "No divisor container" in capsys.readouterr().err
