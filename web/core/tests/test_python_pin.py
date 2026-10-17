"""One Python patch version everywhere: .python-version pins it for local uv, CI and the image (a test that passes on
one patch can fail on another, as 3.14.8's cookie checks did)."""

import re

import yaml

from core.tests.guards import REPO_DIR

PINNED = (REPO_DIR / ".python-version").read_text(encoding="utf-8").strip()

# A python or python3 command that uv doesn't run (uv run reads .python-version itself)
BARE_PYTHON = re.compile(r"(?<!uv run )\bpython3?\b")


def test_the_image_is_built_from_the_pinned_python() -> None:
    """The base image's tag: uv sync in the build refuses a Python other than the pin, but only once it is built."""
    dockerfile = (REPO_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"^FROM python:(\S+?)-slim\b", dockerfile, re.MULTILINE) == [PINNED]


def _pinned(step: dict[str, object]) -> bool:
    uses, inputs = str(step.get("uses", "")), step.get("with") or {}
    assert isinstance(inputs, dict)
    return uses.startswith("actions/setup-python@") and inputs.get("python-version-file") == ".python-version"


def _unpinned_jobs() -> list[str]:
    unpinned = []
    for workflow in sorted((REPO_DIR / ".github" / "workflows").glob("*.yml")):
        for name, job in yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"].items():
            steps = job.get("steps", [])
            for step in steps:
                inputs = step.get("with") or {}
                uv_version = str(inputs.get("python-version", PINNED))
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@") and uv_version != PINNED:
                    unpinned.append(f"{workflow.name}:{name} (setup-uv python-version)")
            bare = any(BARE_PYTHON.search(str(step.get("run", ""))) for step in steps)
            if bare and not any(_pinned(step) for step in steps):
                unpinned.append(f"{workflow.name}:{name}")
    return unpinned


def test_every_workflow_job_that_runs_python_runs_the_pinned_version() -> None:
    """A job's bare python3 is the runner's own (older than 3.14, whose syntax deep_report.py uses) unless the job sets
    up the pinned version; uv run reads .python-version itself. The nightly report job failed on the runner's."""
    assert not _unpinned_jobs(), f"Jobs running Python without the pinned version: {_unpinned_jobs()}"


def test_bare_python_is_told_from_uv_run() -> None:
    assert BARE_PYTHON.search("python3 .github/scripts/deep_report.py")
    assert BARE_PYTHON.search("cd web && python manage.py check")
    assert not BARE_PYTHON.search("uv run python manage.py check")


FROM_RE = re.compile(r"^FROM\s+(\S+?)(?::(\S+))?(?:\s+AS\s+(\S+))?\s*$", re.MULTILINE | re.IGNORECASE)
MOUNT_RE = re.compile(r"--(?:mount=from|from)=([^,\s]+)")
VERSION_TAG = re.compile(r"\d+(\.\d+)+(-\S+)?")


def image_problems(dockerfile: str) -> list[str]:
    """Each image the build uses that Dependabot's docker entry can't bump: a tag that isn't a version (latest), or
    one named only in a --mount=from or COPY --from, which it doesn't read (name a FROM ... AS stage there)."""
    stages = {m.group(3) for m in FROM_RE.finditer(dockerfile) if m.group(3)}
    problems = [
        f"FROM {m.group(1)}:{m.group(2) or ''} (not a version tag)"
        for m in FROM_RE.finditer(dockerfile)
        if not VERSION_TAG.fullmatch(m.group(2) or "")
    ]
    return problems + [f"{image} (not a FROM stage)" for image in MOUNT_RE.findall(dockerfile) if image not in stages]


def test_every_image_the_build_uses_is_one_dependabot_bumps() -> None:
    """The uv mount named ghcr.io/astral-sh/uv:latest: the build's refusals rested on whatever uv was newest, and
    Dependabot's docker entry, with Python ignored, watched nothing."""
    assert image_problems((REPO_DIR / "Dockerfile").read_text(encoding="utf-8")) == []
    updates = yaml.safe_load((REPO_DIR / ".github" / "dependabot.yml").read_text(encoding="utf-8"))["updates"]
    assert any(update["package-ecosystem"] == "docker" for update in updates)


def test_an_unpinned_or_unstaged_image_is_found() -> None:
    assert image_problems("FROM python:3.14.8-slim\nRUN --mount=from=ghcr.io/astral-sh/uv:latest,source=/uv x") == [
        "ghcr.io/astral-sh/uv:latest (not a FROM stage)"
    ]
    assert image_problems("FROM ghcr.io/astral-sh/uv:latest AS uv\nRUN --mount=from=uv,source=/uv x") == [
        "FROM ghcr.io/astral-sh/uv:latest (not a version tag)"
    ]
