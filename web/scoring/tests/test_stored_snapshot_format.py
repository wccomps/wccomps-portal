"""A result frozen in format v1 still renders: the templates show every value a stored snapshot carries for them.

The fixture is a v1 snapshot as Finalize stores it, with made-up values that are each distinct, so a value can only
appear in the page if the template read its key. A template change that stops reading a key fails here; old
results must keep rendering when the templates change.
"""

import json
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from django.template.loader import render_to_string

from scoring.snapshot import context_from_snapshot

FIXTURE = Path(__file__).with_name("fixtures") / "scorecard_snapshot_v1.json"

# Keys the templates keep for logic only (colouring, fallbacks) or don't show; matched against the key path
NOT_SHOWN = re.compile(
    r"\.below_avg$|^stats\.category_ranks\.\w+\.(avg|min|value)$|^stats\.(service|inject)_stats\.\d+\.avg$"
    r"|^stats\.inject_stats\.\d+\.max$"
)
NOT_SHOWN_BY = {
    "scoring/_scorecard_body.html": re.compile(r"^team\.team_number$"),  # the page around the body shows it
    "scoring/scorecard_print.html": re.compile(r"^stats\.neighbors\."),
}


def _values(value: object, path: str = "") -> Iterator[tuple[str, object]]:
    if isinstance(value, dict) and value.keys() == {"$d"}:
        yield path, Decimal(value["$d"])
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _values(item, f"{path}.{key}".lstrip("."))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _values(item, f"{path}.{index}")
    else:
        yield path, value


def _shown_as(value: object) -> str:
    """The value as the templates print it: whole numbers without their sign (the sign may be styled apart)."""
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(abs(int(value)))
    if isinstance(value, int) and not isinstance(value, bool):
        return str(abs(value))
    return str(value)


@pytest.mark.parametrize("template", ["scoring/_scorecard_body.html", "scoring/scorecard_print.html"])
def test_a_stored_v1_snapshot_renders_its_values(template):
    stored = json.loads(FIXTURE.read_text())

    page = render_to_string(template, {**context_from_snapshot(stored), "pdf_url": "/pdf/", "email_url": "/email/"})

    expected = {
        path: _shown_as(value)
        for path, value in _values(stored)
        if not NOT_SHOWN.search(path) and not NOT_SHOWN_BY[template].search(path) and len(_shown_as(value)) > 1
    }
    missing = {
        path: shown for path, shown in expected.items() if not re.search(rf"(?<![\d.]){re.escape(shown)}(?!\d)", page)
    }
    assert len(expected) > 50
    assert missing == {}
