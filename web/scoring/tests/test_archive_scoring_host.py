"""The scoring-host archive script: it takes the right event's divisor log and submissions, never replaces a file in
the archive directory, and its check catches a tarball that no longer matches what was archived."""

import io
import json
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from quotient import archive_scoring_host
from quotient.archive_scoring_host import ArchiveError, archive, main, verify

LOG = 'time=2026-10-31T18:24:18.305Z level=DEBUG msg="Added SNAT rule" ip=192.0.2.5\n'


@pytest.fixture
def docker(tmp_path):
    """A docker that knows one divisor container, started from FAKE_EVENT_DIR, and prints LOG (plus a stderr line)
    as its logs."""
    path = tmp_path / "docker"
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'ps':\n"
        "    labels = [a.split('=', 1)[1] for a in args if a.startswith('label=')]\n"
        "    if f\"com.docker.compose.project.working_dir={os.environ['FAKE_EVENT_DIR']}\" in labels:\n"
        "        print('c0ffee')\n"
        "elif args[:2] == ['logs', 'c0ffee']:\n"
        f"    sys.stdout.write({LOG!r}); sys.stdout.flush(); sys.stderr.write('stderr line\\n')\n"
        "else:\n"
        "    sys.exit('no such container')\n"
    )
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def event_dir(tmp_path, monkeypatch):
    event = tmp_path / "events" / "2027-inv9-example"
    (event / "submissions" / "2" / "38").mkdir(parents=True)
    (event / "submissions" / "2" / "38" / "1").write_bytes(b"inject 02, team 38, attempt 1")
    monkeypatch.setenv("FAKE_EVENT_DIR", str(event.resolve()))
    return event


@pytest.fixture
def archive_dir(tmp_path):
    path = tmp_path / "archive"
    path.mkdir()
    return path


def test_archive_holds_the_events_submissions_and_divisor_log(event_dir, archive_dir, docker):
    target = archive(event_dir, archive_dir, docker)

    assert sorted(p.name for p in archive_dir.iterdir()) == [target.name, f"{target.name}.sha256"]
    assert target.name.startswith("2027-inv9-example-scoring-host-")
    with tarfile.open(target, "r:gz") as tar:
        log = tar.extractfile("divisor.log").read().decode()
        submission = tar.extractfile("submissions/2/38/1").read()
        manifest = json.load(tar.extractfile("manifest.json"))
    assert log == LOG + "stderr line\n"
    assert submission == b"inject 02, team 38, attempt 1"
    assert manifest["event"] == "2027-inv9-example"
    assert manifest["divisor_container"] == "c0ffee"
    assert set(manifest["files"]) == {"divisor.log", "submissions/2/38/1"}
    assert verify(target) == []


def test_another_events_divisor_is_not_taken(event_dir, archive_dir, docker, monkeypatch):
    monkeypatch.setenv("FAKE_EVENT_DIR", "/elsewhere/2027-inv8-other")

    with pytest.raises(ArchiveError, match="No divisor container was started from"):
        archive(event_dir, archive_dir, docker)
    assert list(archive_dir.iterdir()) == []


def test_a_failed_log_read_leaves_nothing(event_dir, archive_dir, tmp_path):
    failing = tmp_path / "failing-docker"
    failing.write_text(f"#!{sys.executable}\nimport sys\nprint('c0ffee') if sys.argv[1] == 'ps' else sys.exit(3)\n")
    failing.chmod(0o755)

    with pytest.raises(ArchiveError, match="docker logs c0ffee failed"):
        archive(event_dir, archive_dir, str(failing))
    assert list(archive_dir.iterdir()) == []


def test_an_existing_file_is_never_replaced(event_dir, archive_dir, docker, monkeypatch):
    fixed = datetime(2027, 2, 6, 23, 0, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(archive_scoring_host, "datetime", Frozen)
    first = archive(event_dir, archive_dir, docker)
    kept = first.read_bytes()

    with pytest.raises(ArchiveError, match="already exists"):
        archive(event_dir, archive_dir, docker)
    assert first.read_bytes() == kept
    assert sorted(p.name for p in archive_dir.iterdir()) == [first.name, f"{first.name}.sha256"]


def test_verify_catches_a_changed_copy(event_dir, archive_dir, docker):
    target = archive(event_dir, archive_dir, docker)
    checksum = target.with_name(f"{target.name}.sha256")

    # A file swapped inside a rebuilt tarball, with a matching .sha256: only the manifest can tell
    rebuilt = io.BytesIO()
    with tarfile.open(target, "r:gz") as old, tarfile.open(fileobj=rebuilt, mode="w:gz") as new:
        for member in old.getmembers():
            data = old.extractfile(member).read() if member.isfile() else None
            if member.name == "divisor.log":
                data = b"edited\n"
                member.size = len(data)
            new.addfile(member, io.BytesIO(data) if data is not None else None)
    target.write_bytes(rebuilt.getvalue())
    checksum.write_text(f"{archive_scoring_host.sha256_of(target)}  {target.name}\n")
    assert verify(target) == ["divisor.log does not match its manifest entry"]

    target.write_bytes(b"truncated")
    assert verify(target) == [f"{target.name} does not match {checksum.name}"]


def test_the_command_reports_the_tarball_or_why_not(event_dir, archive_dir, docker, capsys, monkeypatch):
    assert main(["archive", str(event_dir), str(archive_dir), "--docker", docker]) == 0
    printed = capsys.readouterr().out.split()
    assert Path(printed[0]).parent == archive_dir
    assert main(["verify", printed[0]]) == 0

    monkeypatch.setenv("FAKE_EVENT_DIR", "/elsewhere")
    assert main(["archive", str(event_dir), str(archive_dir), "--docker", docker]) == 1
    assert "No divisor container" in capsys.readouterr().err
