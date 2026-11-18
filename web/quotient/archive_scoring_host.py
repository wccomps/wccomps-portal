#!/usr/bin/env python3
"""Archive a Quotient event's inject submissions and its divisor log from the scoring host, before the event's
files or containers are recreated for the next one. Close Out archives the portal's side; these two live only on the
scoring host.

Run it on the scoring host, as a user that can read the event directory and run docker, once the event is over:

    python3 archive_scoring_host.py archive <event's Compose directory> <archive directory>
    python3 archive_scoring_host.py verify <archive directory>/<event>-scoring-host-<UTC time>.tar.gz

archive writes <event>-scoring-host-<UTC time>.tar.gz and its .sha256 into the archive directory, and never replaces
a file there. The tarball holds:
- submissions/: the event directory's submissions (inject uploads, by inject, team and attempt);
- divisor.log: `docker logs` of the event's divisor container, the source addresses the runners used and when
  (the container is found by its Compose labels, so another event's divisor is never taken for this one);
- manifest.json: the event, when and from which container, and each file's size and SHA-256.
verify checks a copy, wherever it was moved, against its .sha256 file and its manifest.

Standard library only, and Python 3.11 or later: the scoring host doesn't have the portal's environment.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SUBMISSIONS = "submissions"
DIVISOR_LOG = "divisor.log"
MANIFEST = "manifest.json"
CHUNK = 1024 * 1024


class ArchiveError(Exception):
    """What stopped the archive or failed the check; nothing was left in the archive directory."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def divisor_container(docker: str, event_dir: Path) -> str:
    """The id of the event's divisor container: the one Compose started from event_dir as service "divisor"."""
    listed = subprocess.run(  # noqa: S603
        [
            docker,
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project.working_dir={event_dir}",
            "--filter",
            "label=com.docker.compose.service=divisor",
            "--format",
            "{{.ID}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise ArchiveError(f"docker ps failed: {listed.stderr.strip()}")
    ids = listed.stdout.split()
    if not ids:
        raise ArchiveError(
            f"No divisor container was started from {event_dir}: its log went with it if it was removed or recreated"
        )
    if len(ids) > 1:
        raise ArchiveError(f"Several divisor containers were started from {event_dir}: {', '.join(ids)}")
    return ids[0]


def write_divisor_log(docker: str, container: str, path: Path) -> None:
    """The container's whole log, its stdout and stderr interleaved as docker returns them."""
    with path.open("wb") as file:
        result = subprocess.run(  # noqa: S603
            [docker, "logs", container], stdout=file, stderr=subprocess.STDOUT, check=False
        )
    if result.returncode != 0:
        raise ArchiveError(f"docker logs {container} failed: {path.read_text(errors='replace').strip()}")


def build_manifest(
    event: str, container: str, submissions: Path, divisor_log: Path, created_at: str
) -> dict[str, object]:
    files: dict[str, dict[str, object]] = {}
    # The tarball stores a symlink as a link, so the manifest lists only regular files
    for path in sorted(p for p in submissions.rglob("*") if p.is_file() and not p.is_symlink()):
        files[f"{SUBMISSIONS}/{path.relative_to(submissions).as_posix()}"] = {
            "size": path.stat().st_size,
            "sha256": sha256_of(path),
        }
    files[DIVISOR_LOG] = {"size": divisor_log.stat().st_size, "sha256": sha256_of(divisor_log)}
    return {"event": event, "created_at": created_at, "divisor_container": container, "files": files}


def _link_new(source: Path, target: Path) -> None:
    """Give source the name target, refusing if target exists (a hard link never replaces a file)."""
    try:
        os.link(source, target)
    except FileExistsError:
        raise ArchiveError(f"{target} already exists; nothing was replaced") from None


def archive(event_dir: Path, archive_dir: Path, docker: str = "docker") -> Path:
    """Write the event's tarball and its .sha256 into archive_dir and return the tarball's path."""
    event_dir = event_dir.resolve()
    submissions = event_dir / SUBMISSIONS
    if not submissions.is_dir():
        raise ArchiveError(f"{submissions} is not a directory")
    if not archive_dir.is_dir():
        raise ArchiveError(f"{archive_dir} is not a directory")
    container = divisor_container(docker, event_dir)
    now = datetime.now(UTC)
    target = archive_dir / f"{event_dir.name}-scoring-host-{now:%Y%m%dT%H%M%SZ}.tar.gz"
    checksum = target.with_name(f"{target.name}.sha256")
    if target.exists() or checksum.exists():
        raise ArchiveError(f"{target} or its .sha256 already exists; nothing was replaced")

    with tempfile.TemporaryDirectory(dir=archive_dir) as scratch:
        work = Path(scratch)
        divisor_log = work / DIVISOR_LOG
        write_divisor_log(docker, container, divisor_log)
        manifest = build_manifest(event_dir.name, container, submissions, divisor_log, now.isoformat())
        manifest_path = work / MANIFEST
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        tarball = work / target.name
        with tarfile.open(tarball, "w:gz") as tar:
            tar.add(manifest_path, arcname=MANIFEST)
            tar.add(divisor_log, arcname=DIVISOR_LOG)
            tar.add(submissions, arcname=SUBMISSIONS)
        digest = sha256_of(tarball)
        _link_new(tarball, target)
        try:
            with checksum.open("x") as file:
                file.write(f"{digest}  {target.name}\n")
        except FileExistsError:
            target.unlink()
            raise ArchiveError(f"{checksum} already exists; nothing was replaced") from None
    return target


def verify(tarball: Path) -> list[str]:
    """What is wrong with the tarball against its .sha256 and its manifest; empty when it matches."""
    checksum = tarball.with_name(f"{tarball.name}.sha256")
    if not checksum.is_file():
        return [f"{checksum} is missing"]
    expected = checksum.read_text().split()[0]
    if sha256_of(tarball) != expected:
        return [f"{tarball.name} does not match {checksum.name}"]
    with tarfile.open(tarball, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        manifest_member = members.pop(MANIFEST, None)
        if manifest_member is None:
            return [f"{tarball.name} has no {MANIFEST}"]
        extracted = tar.extractfile(manifest_member)
        files = json.load(extracted)["files"] if extracted else {}
        problems = [f"{name} is in the manifest but not in the tarball" for name in sorted(set(files) - set(members))]
        problems += [f"{name} is in the tarball but not in the manifest" for name in sorted(set(members) - set(files))]
        for name in sorted(set(files) & set(members)):
            data = tar.extractfile(members[name])
            digest = hashlib.sha256(data.read() if data else b"").hexdigest()
            if digest != files[name]["sha256"]:
                problems.append(f"{name} does not match its manifest entry")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("archive", help="archive an event's submissions and divisor log")
    make.add_argument("event_dir", type=Path, help="the event's Compose directory, holding submissions/")
    make.add_argument("archive_dir", type=Path, help="an existing directory to write the tarball into")
    make.add_argument("--docker", default="docker", help="the docker command (default: docker)")
    check = commands.add_parser("verify", help="check a tarball against its .sha256 and manifest")
    check.add_argument("tarball", type=Path)
    args = parser.parse_args(argv)

    if args.command == "verify":
        problems = verify(args.tarball)
        for problem in problems:
            print(problem, file=sys.stderr)
        if not problems:
            print(f"{args.tarball}: matches its .sha256 and manifest")
        return 1 if problems else 0
    try:
        target = archive(args.event_dir, args.archive_dir, args.docker)
    except ArchiveError as e:
        print(e, file=sys.stderr)
        return 1
    problems = verify(target)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"{target}\n{target.with_name(f'{target.name}.sha256').read_text().split()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
