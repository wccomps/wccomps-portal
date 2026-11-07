"""The status-to-variant filter: a known kind maps; a mistyped kind can't take a page down in production."""

import logging
from decimal import Decimal

import pytest
from django.template import Context, Template

from core.status_variants import badge_variant, score_class


def test_a_stored_status_maps_to_its_variant() -> None:
    rendered = Template("{% load status_variants %}{{ s|badge_variant:'packet_email' }}").render(
        Context({"s": "failed"})
    )
    assert rendered == "danger"


def test_an_unknown_kind_raises_under_debug(settings) -> None:
    settings.DEBUG = True
    with pytest.raises(ValueError, match="tickets"):
        badge_variant("open", "tickets")


def test_an_unknown_kind_is_logged_and_renders_the_default_badge_in_production(settings, caplog) -> None:
    settings.DEBUG = False
    with caplog.at_level(logging.ERROR, logger="core.status_variants"):
        assert badge_variant("open", "tickets") == "default"
    assert "tickets" in caplog.text


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        ("1", "score-positive"),
        ("-1", "score-negative"),
        ("0", "score-zero"),
        ("0.00", "score-zero"),
        ("-0.00", "score-zero"),
        ("0.01", "score-positive"),
        (Decimal("2.50"), "score-positive"),
        ("12345678901234567890123456790", "score-positive"),
        ("-1e30", "score-negative"),
        ("1e1000000", "score-positive"),
        (Decimal("NaN"), "score-zero"),
        (Decimal("Infinity"), "score-zero"),
        ("", "score-zero"),
        (None, "score-zero"),
        ("not a number", "score-zero"),
    ],
)
def test_a_score_takes_the_class_of_the_text_shown(shown: object, expected: str) -> None:
    """The sign of the text the page shows; anything that isn't a finite number is zero."""
    assert score_class(shown) == expected
