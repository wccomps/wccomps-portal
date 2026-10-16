"""The nightly deep property run's issue body, from the tests' JUnit XML: python deep_report.py RESULTS RUN_URL SHA.

Each failing test and its message (Hypothesis's falsifying example) go in one code block, fenced with a longer run
of backticks than any in the text, so a payload can't close it and nothing in it becomes a mention or markup.
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LIMIT = 60_000  # GitHub refuses an issue or comment body over 65536 characters


def failures(results: Path) -> list[str]:
    """Each failing or erroring test with its message (its last 3000 characters); a missing or unreadable results
    file is one failure saying so."""
    try:
        cases = ET.parse(results).getroot().iter("testcase")  # noqa: S314 - the run's own pytest output
    except OSError, ET.ParseError:
        return ["The run left no readable test results (an earlier step failed, or the job timed out): see the run."]
    found = []
    for case in cases:
        for problem in (*case.findall("failure"), *case.findall("error")):
            text = (problem.text or problem.get("message") or "")[-3000:]
            found.append(f"{case.get('classname')}::{case.get('name')}\n{text}")
    return found


def fenced(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def body(results: Path, run_url: str, sha: str) -> str:
    found = failures(results) or ["The tests failed, but the results name no failing test: see the run."]
    report = fenced("\n\n".join(found)[: LIMIT - 1000])
    return f"Run: {run_url}\nCommit: {sha}\n\n{report}\n"


if __name__ == "__main__":
    sys.stdout.write(body(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
