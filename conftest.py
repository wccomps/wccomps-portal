"""Hypothesis profiles for every test under the repo, chosen by HYPOTHESIS_PROFILE (default "ci")."""

import os

from hypothesis import HealthCheck, settings

# Property tests take pytest fixtures once per test and roll each example back themselves
# (core.tests.strategies.rolled_back), so the function-scoped fixture check doesn't apply. Hostile inputs are
# slow and large on purpose (5000-character strings, files), so the speed and size checks don't either.
_SUPPRESSED = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large]

# Each profile, and its "-views" variant for the view property tests (web/core/tests/views_harness.py), which send
# a whole request per example for every URL pattern and so run fewer.
# ci: the same examples every run, few enough that the whole suite stays fast. deep: many random examples, for a
# search before a release or after changing input handling.
PROFILES: dict[str, tuple[dict[str, object], int]] = {
    "ci": ({"max_examples": 25, "derandomize": True, "database": None}, 10),
    "deep": ({"max_examples": 2000}, 500),
}
for _name, (_options, _view_examples) in PROFILES.items():
    settings.register_profile(_name, deadline=None, suppress_health_check=_SUPPRESSED, **_options)
    settings.register_profile(f"{_name}-views", parent=settings.get_profile(_name), max_examples=_view_examples)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
