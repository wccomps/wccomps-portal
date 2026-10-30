"""The image runs web and bot as a non-root user, with a numeric uid Kubernetes' runAsNonRoot can check."""

import re
from pathlib import PurePosixPath

from core.tests.guards import REPO_DIR

USER_RE = re.compile(r"^USER\s+(\S+)\s*$", re.MULTILINE)


def test_the_image_runs_as_a_numeric_non_root_user() -> None:
    users = USER_RE.findall((REPO_DIR / "Dockerfile").read_text(encoding="utf-8"))
    assert users, "The Dockerfile sets no USER, so web and bot run as root"
    uid = users[-1].split(":")[0]
    assert uid.isdigit(), f"USER {users[-1]}: runAsNonRoot can only verify a numeric uid"
    assert int(uid) != 0


def test_the_image_user_owns_the_bot_heartbeat_directory() -> None:
    """The bot writes its heartbeats there; a non-root user can't create it under the root-owned /app."""
    from bot.heartbeat import DEFAULT_DIR

    owned = re.findall(r"\bchown\s+\S+\s+(\S+)", (REPO_DIR / "Dockerfile").read_text(encoding="utf-8"))
    assert any(PurePosixPath(DEFAULT_DIR).is_relative_to(directory) for directory in owned), owned
