#!/usr/bin/env python3
"""Archive a Quotient event's inject submissions and divisor log from the scoring host.

Close Out archives the portal's side of an event; these two live only on the scoring host, where the next event's
containers replace the divisor's log. Run this there, as a user that can read the event directory and run docker,
once the event is over and before its containers are recreated:

    python3 archive_scoring_host.py archive <event's Compose directory> <archive directory>
    python3 archive_scoring_host.py verify <archive directory>/<event>-scoring-host-<UTC time>.tar.gz

archive writes <event>-scoring-host-<UTC time>.tar.gz and its .sha256 into the archive directory, readable only by
their owner, and never replaces a file there (it names them by hard link, so the directory's filesystem must support
hard links). The tarball holds:
- submissions/: every regular file under the event directory's submissions (inject uploads, by inject, team and
  attempt). Symlinks below it are left out and listed in the manifest: following one would let whoever can write
  there choose any file on the host. A submissions directory that is itself a symlink is followed;
- divisor.log: `docker logs` of the event's divisor container, the source addresses the runners used and when
  (the container is found by its Compose labels, so another event's divisor is never taken for this one);
- manifest.json: the event, when and from which container, each file's size and SHA-256, and the links left out.
verify checks a copy, wherever it was moved, against its .sha256 file and its manifest.

Standard library only, and Python 3.11 or later: the scoring host doesn't have the portal's environment.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

SUBMISSIONS = "submissions"
DIVISOR_LOG = "divisor.log"
MANIFEST = "manifest.json"
WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"


class ArchiveError(Exception):
    """What stopped the archive; nothing was left in the archive directory."""


def sha256_of(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def checksum_path(tarball: Path) -> Path:
    return tarball.with_name(f"{tarball.name}.sha256")


def divisor_container(event_dir: Path) -> str:
    """The id of the event's divisor container: the one Compose started from event_dir as service "divisor". The
    label holds the directory as Compose was given it, so both sides are compared with their symlinks resolved."""
    listed = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "ps",
            "--all",
            "--filter",
            "label=com.docker.compose.service=divisor",
            "--format",
            f'{{{{.ID}}}} {{{{.Label "{WORKING_DIR_LABEL}"}}}}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise ArchiveError(f"docker ps failed: {listed.stderr.strip()}")
    ids = [
        container
        for container, _, working_dir in (line.partition(" ") for line in listed.stdout.splitlines())
        if working_dir and Path(working_dir).resolve() == event_dir
    ]
    if not ids:
        raise ArchiveError(
            f"No divisor container was started from {event_dir}: its log went with it if it was removed or recreated"
        )
    if len(ids) > 1:
        raise ArchiveError(f"Several divisor containers were started from {event_dir}: {', '.join(ids)}")
    return ids[0]


def write_divisor_log(container: str, path: Path) -> None:
    """The container's whole log, its stdout and stderr interleaved as docker returns them."""
    with path.open("wb") as file:
        result = subprocess.run(  # noqa: S603
            ["docker", "logs", container],  # noqa: S607
            stdout=file,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode != 0:
        raise ArchiveError(f"docker logs {container} failed: {path.read_text(errors='replace').strip()}")


def _add(tar: tarfile.TarFile, path: Path, arcname: str) -> dict[str, object]:
    """Store the regular file's contents under arcname and return its manifest entry."""
    info = tarfile.TarInfo(arcname)
    stat = path.stat()
    info.size, info.mtime, info.mode = stat.st_size, int(stat.st_mtime), 0o600
    with path.open("rb") as file:
        tar.addfile(info, file)
    return {"size": info.size, "sha256": sha256_of(path)}


def _submissions(root: Path) -> tuple[list[Path], list[str]]:
    """Every regular file under root, and the symlinks below it (left out, relative to root). A directory that can't
    be read stops the archive rather than leaving its files out unnoticed."""

    def refuse(error: OSError) -> None:
        raise ArchiveError(f"Could not read {error.filename}: {error.strerror}")

    files, links = [], []
    for directory, subdirectories, names in os.walk(root, onerror=refuse):
        for name in [*subdirectories, *names]:
            path = Path(directory) / name
            if path.is_symlink():
                links.append(path.relative_to(root).as_posix())
            elif name in names and path.is_file():
                files.append(path)
    return sorted(files), sorted(links)


def archive(event_dir: Path, archive_dir: Path) -> tuple[Path, str, list[str]]:
    """Write the event's tarball and its .sha256 into archive_dir; return the tarball's path, its SHA-256 and the
    symlinks under submissions/ it left out."""
    event_dir = event_dir.resolve()
    submissions = (event_dir / SUBMISSIONS).resolve()
    if not submissions.is_dir():
        raise ArchiveError(f"{event_dir / SUBMISSIONS} is not a directory")
    if not archive_dir.is_dir():
        raise ArchiveError(f"{archive_dir} is not a directory")
    container = divisor_container(event_dir)
    now = datetime.now(UTC)
    target = archive_dir / f"{event_dir.name}-scoring-host-{now:%Y%m%dT%H%M%SZ}.tar.gz"
    checksum = checksum_path(target)
    if target.exists() or checksum.exists():
        raise ArchiveError(f"{target} or its .sha256 already exists; nothing was replaced")

    with tempfile.TemporaryDirectory(dir=archive_dir) as scratch:
        work = Path(scratch)
        divisor_log = work / DIVISOR_LOG
        write_divisor_log(container, divisor_log)
        tarball = work / target.name
        paths, links = _submissions(submissions)
        try:
            with tarfile.open(tarball, "w:gz") as tar:
                files = {DIVISOR_LOG: _add(tar, divisor_log, DIVISOR_LOG)}
                for path in paths:
                    arcname = f"{SUBMISSIONS}/{path.relative_to(submissions).as_posix()}"
                    files[arcname] = _add(tar, path, arcname)
                manifest = {
                    "event": event_dir.name,
                    "created_at": now.isoformat(),
                    "divisor_container": container,
                    "files": files,
                    "links_left_out": links,
                }
                data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
                info = tarfile.TarInfo(MANIFEST)
                info.size, info.mtime, info.mode = len(data), int(now.timestamp()), 0o600
                tar.addfile(info, io.BytesIO(data))
            tarball.chmod(0o600)
            digest = sha256_of(tarball)
            sidecar = work / checksum.name
            sidecar.write_text(f"{digest}  {target.name}\n")
            sidecar.chmod(0o600)
        except OSError as e:
            raise ArchiveError(f"Could not build the tarball: {e}; nothing was kept") from None
        # A hard link never replaces a file: if another run took either name meanwhile, this one gives up
        try:
            os.link(tarball, target)
        except FileExistsError:
            raise ArchiveError(f"{target} already exists; nothing was replaced") from None
        except OSError as e:
            raise ArchiveError(f"Could not name {target} ({e}); the archive directory needs hard links") from None
        try:
            os.link(sidecar, checksum)
        except OSError as e:
            target.unlink()
            raise ArchiveError(f"Could not write {checksum}: {e}; nothing was kept") from None
    return target, digest, links


def verify(tarball: Path) -> list[str]:
    """What is wrong with the tarball against its .sha256 and its manifest; empty when it matches."""
    checksum = checksum_path(tarball)
    try:
        expected = checksum.read_text().split()
        if not expected:
            return [f"{checksum.name} is empty"]
        if sha256_of(tarball) != expected[0]:
            return [f"{tarball.name} does not match {checksum.name}"]
        with tarfile.open(tarball, "r:gz") as tar:
            members = {m.name: m for m in tar.getmembers() if m.isfile()}
            if MANIFEST not in members:
                return [f"{tarball.name} has no {MANIFEST}"]

            def read(member: tarfile.TarInfo) -> io.BufferedReader:
                return cast(io.BufferedReader, tar.extractfile(member))  # a regular member always has a file

            files = json.load(read(members.pop(MANIFEST)))["files"]
            problems = [f"{name} is in the manifest but not in the tarball" for name in sorted(files.keys() - members)]
            problems += [f"{name} is in the tarball but not in the manifest" for name in sorted(members.keys() - files)]
            for name in sorted(files.keys() & members.keys()):
                if hashlib.file_digest(read(members[name]), "sha256").hexdigest() != files[name]["sha256"]:
                    problems.append(f"{name} does not match its manifest entry")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError) as e:
        return [f"Could not read {tarball.name}: {e}"]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("archive", help="archive an event's submissions and divisor log")
    make.add_argument("event_dir", type=Path, help="the event's Compose directory, holding submissions/")
    make.add_argument("archive_dir", type=Path, help="an existing directory to write the tarball into")
    check = commands.add_parser("verify", help="check a tarball against its .sha256 and manifest")
    check.add_argument("tarball", type=Path)
    args = parser.parse_args(argv)

    if args.command == "verify":
        problems = verify(args.tarball)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        print(f"{args.tarball}: matches its .sha256 and manifest")
        return 0
    try:
        target, digest, links = archive(args.event_dir, args.archive_dir)
    except ArchiveError as e:
        print(e, file=sys.stderr)
        return 1
    for link in links:
        print(f"Left out the symlink submissions/{link}", file=sys.stderr)
    print(f"{target}\n{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
