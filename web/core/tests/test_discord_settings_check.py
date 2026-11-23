"""Unset Discord IDs must be reported loudly, not silently treated as 'not found'.

On 2026-03-07 the settings defaults changed from real IDs to 0 and the production .env never got
the values, so role sync, /link roles and announcement broadcasts silently did nothing for months.
"""

import pytest

from core.admin_views.readiness import _check_discord_settings, checks
from core.utils import REQUIRED_DISCORD_SETTINGS, missing_discord_settings


@pytest.fixture
def all_set(settings):
    for i, name in enumerate(REQUIRED_DISCORD_SETTINGS, start=1):
        setattr(settings, name, 1000 + i)
    return settings


def test_nothing_missing_when_all_set(all_set):
    assert missing_discord_settings() == []


def test_zero_ids_are_reported_by_env_var_name(all_set):
    all_set.GOLDTEAM_ROLE_ID = 0
    all_set.COMPETITION_GUILD_ID = 0

    missing = missing_discord_settings()

    assert "GOLDTEAM_ROLE_ID" in missing
    assert "DISCORD_GUILD_ID" in missing  # env var name, not the Python setting name
    assert len(missing) == 2


def test_optional_panel_channels_are_not_required(all_set):
    all_set.DISCORD_LINK_CHANNEL_ID = 0
    all_set.DISCORD_WELCOME_CHANNEL_ID = 0

    assert missing_discord_settings() == []


def test_readiness_fails_listing_missing_ids(all_set):
    all_set.BLUETEAM_ROLE_ID = 0
    all_set.DISCORD_ANNOUNCEMENT_CHANNEL_ID = 0

    severity, detail, _ = _check_discord_settings()

    assert severity == "fail"
    assert "BLUETEAM_ROLE_ID" in detail and "DISCORD_ANNOUNCEMENT_CHANNEL_ID" in detail


def test_readiness_passes_when_all_set(all_set):
    severity, _, _ = _check_discord_settings()
    assert severity == "pass"


def test_readiness_runs_the_check():
    assert any(fn is _check_discord_settings for _, fn in checks())
