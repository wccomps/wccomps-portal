"""The status-to-variant filter: a known kind maps; a mistyped kind can't take a page down in production."""

import logging

import pytest
from django.template import Context, Template

from core.status_variants import badge_variant


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
