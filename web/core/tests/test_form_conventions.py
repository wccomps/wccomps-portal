"""Static checks that keep page templates on the shared form and message conventions.

Exceptions must be visible in this file, with a reason, so they get reviewed.
"""

import re
from pathlib import Path

import pytest

from core.tests.guards import is_standalone_document, template_name, tracked_templates


def all_page_templates() -> list[Path]:
    """Every template except cotton components and standalone documents (emails, the public registration form)."""
    return [p for p in tracked_templates() if "cotton" not in p.parts and not is_standalone_document(p)]


def rel(path: Path) -> str:
    return template_name(path)


MESSAGE_LOOP = re.compile(r"{%\s*if\s+messages\s*%}|{%\s*for\s+\w+\s+in\s+messages\s*%}")

# Files allowed to loop over messages themselves, with the reason.
MESSAGE_LOOP_ALLOWLIST = {
    "admin/base_site.html": "the one place messages render",
}


def test_pages_do_not_render_messages_themselves() -> None:
    """The base template renders messages; a page loop would show every message twice."""
    offenders = [
        rel(p)
        for p in all_page_templates()
        if rel(p) not in MESSAGE_LOOP_ALLOWLIST and MESSAGE_LOOP.search(p.read_text())
    ]
    if offenders:
        pytest.fail(
            "Templates loop over messages; delete the loop (admin/base_site.html renders them):\n"
            + "\n".join(f"  - {t}" for t in offenders)
        )
