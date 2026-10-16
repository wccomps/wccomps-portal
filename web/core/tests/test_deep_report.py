"""The nightly deep property run's issue body (.github/scripts/deep_report.py)."""

import importlib.util
import re
from pathlib import Path

from hypothesis import example, given
from hypothesis import strategies as st

from core.tests.guards import REPO_DIR
from core.tests.strategies import request_text

_spec = importlib.util.spec_from_file_location("deep_report", REPO_DIR / ".github/scripts/deep_report.py")
assert _spec is not None and _spec.loader is not None
deep_report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deep_report)


def _junit(tmp: Path, message: str) -> Path:
    from xml.sax.saxutils import escape

    path = tmp / "results.xml"
    path.write_text(
        '<testsuites><testsuite><testcase classname="web.core.tests.test_x" name="test_y[1]">'
        f"<failure>{escape(message)}</failure></testcase>"
        '<testcase classname="a" name="passes"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _outside_the_fence(body: str) -> str:
    fence = re.search(r"^(`{3,})text$", body, re.MULTILINE)
    assert fence, "no code block"
    start = fence.end()
    end = body.index("\n" + fence.group(1) + "\n", start)
    return body[: fence.start()] + body[end + len(fence.group(1)) + 2 :]


@given(message=st.one_of(request_text(200), st.sampled_from(["```", "````\n@everyone", "x`````y <@&1>"])))
@example(message="```\n@everyone ran **here**\n```")
def test_a_failure_stays_inside_its_code_block(tmp_path_factory, message: str) -> None:
    """Falsifying examples print payloads: three backticks closed the old fence and an @name became a mention."""
    # pytest writes JUnit XML, which can't hold control characters (it escapes them), and XML reads CR as LF
    message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\r]", "", message)
    body = deep_report.body(_junit(tmp_path_factory.mktemp("r"), message), "https://run", "abc123")
    outside = _outside_the_fence(body)
    assert outside == "Run: https://run\nCommit: abc123\n\n", outside
    assert "web.core.tests.test_x::test_y[1]" in body
    assert len(body) < 65_536


def test_a_missing_results_file_is_reported(tmp_path: Path) -> None:
    body = deep_report.body(tmp_path / "none.xml", "https://run", "abc123")
    assert "no readable test results" in body


def test_every_workflow_job_that_runs_python_sets_up_the_pinned_version() -> None:
    """A job's python3 is the runner's own (older than 3.14, which the scripts' syntax needs) unless the job sets up
    the version .python-version pins: the report job failed on the runner's python3."""
    import yaml

    unpinned = []
    for workflow in sorted((REPO_DIR / ".github" / "workflows").glob("*.yml")):
        for name, job in yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"].items():
            steps = job.get("steps", [])
            runs_python = any(re.search(r"\bpython3?\b", step.get("run", "")) for step in steps)
            pinned = any(
                step.get("uses", "").startswith("actions/setup-python@")
                and step.get("with", {}).get("python-version-file") == ".python-version"
                for step in steps
            )
            if runs_python and not pinned:
                unpinned.append(f"{workflow.name}:{name}")
    assert not unpinned, f"Jobs running Python without the pinned version: {unpinned}"
