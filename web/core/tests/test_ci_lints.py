"""tests.yml runs every tool CLAUDE.md's lint line lists."""

import re

import yaml

from core.tests.guards import REPO_DIR

LINT_LINE = re.compile(r"^uv run ruff check \. && .*$", re.MULTILINE)


TOOL = re.compile(r"\buv run (\S+)")


def documented_tools() -> list[str]:
    line = LINT_LINE.search((REPO_DIR / "CLAUDE.md").read_text(encoding="utf-8"))
    assert line, "CLAUDE.md's lint line moved; update LINT_LINE"
    return TOOL.findall(line.group(0))


def ci_tools() -> set[str]:
    workflow = yaml.safe_load((REPO_DIR / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["lint-and-test"]["steps"]
    return {tool for step in steps for tool in TOOL.findall(str(step.get("run", "")))}


def test_ci_runs_every_documented_lint() -> None:
    assert documented_tools(), "no uv run commands found on the lint line"
    missing = [tool for tool in documented_tools() if tool not in ci_tools()]
    assert not missing, f"CLAUDE.md says CI runs these, but tests.yml doesn't: {missing}"
