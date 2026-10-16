"""Lint test: all POST data must go through Django forms.

Raw request.POST access bypasses CharField max_length validation and other
form-level protections. This test ensures views always use Django form classes.
"""

import fnmatch
import re

from core.tests.guards import REPO_DIR, fail_with, tracked_files

# Patterns that indicate raw POST data extraction
RAW_POST_PATTERNS = re.compile(
    r"""
    request\.POST\.get\(          # request.POST.get("field")
    | request\.POST\.getlist\(    # request.POST.getlist("field")
    | request\.POST\[             # request.POST["field"]
    """,
    re.VERBOSE,
)

# The view modules scanned (repo-relative fnmatch patterns over tracked files)
VIEW_PATTERNS = ("web/*/views.py", "web/*/views/*.py", "web/*/admin_views/*.py")

# "path" -> why that view reads request.POST itself
EXCEPTIONS: dict[str, str] = {}


def test_no_raw_post_access() -> None:
    """Views must use Django forms instead of raw request.POST access."""
    found = {}
    for name in tracked_files():
        if not any(fnmatch.fnmatch(name, pattern) for pattern in VIEW_PATTERNS):
            continue
        for number, line in enumerate((REPO_DIR / name).read_text().splitlines(), 1):
            code = line.split("#")[0]
            if RAW_POST_PATTERNS.search(code):
                found[f"{name}:{number}"] = line.strip()
    fail_with("Raw request.POST access (use a Django form class)", found, EXCEPTIONS)
