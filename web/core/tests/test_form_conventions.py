"""Static checks that keep page templates on the shared form and message conventions.

Exceptions must be visible in this file, with a reason, so they get reviewed.
"""

import re

from core.tests.guards import fail_with, page_templates, template_name, text

MESSAGE_LOOP = re.compile(r"{%\s*if\s+messages\s*%}|{%\s*for\s+\w+\s+in\s+messages\s*%}")

# Files allowed to loop over messages themselves, with the reason.
MESSAGE_LOOP_ALLOWLIST = {
    "admin/base_site.html": "the one place messages render",
}


def test_pages_do_not_render_messages_themselves() -> None:
    """The base template renders messages; a page loop would show every message twice."""
    found = {
        template_name(p): "loops over messages; admin/base_site.html renders them"
        for p in page_templates()
        if MESSAGE_LOOP.search(text(p))
    }
    fail_with("Pages that render messages themselves", found, MESSAGE_LOOP_ALLOWLIST)
