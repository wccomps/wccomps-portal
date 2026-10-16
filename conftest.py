"""Hypothesis profiles for every test under the repo, chosen by HYPOTHESIS_PROFILE (default "ci")."""

import os

from hypothesis import HealthCheck, settings

# Property tests take pytest fixtures once per test and roll each example back themselves
# (core.tests.strategies.rolled_back), so the function-scoped fixture check doesn't apply. Hostile inputs are
# slow and large on purpose (5000-character strings, files), so the speed and size checks don't either.
_SUPPRESSED = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large]

# ci: fixed examples, few enough that the whole suite stays fast; deep: random ones (nightly on main). Each has a
# "-views" variant with fewer, for the view property tests, which send a whole request per example.
settings.register_profile(
    "ci", max_examples=25, derandomize=True, deadline=None, database=None, suppress_health_check=_SUPPRESSED
)
settings.register_profile("deep", max_examples=2000, deadline=None, suppress_health_check=_SUPPRESSED)
settings.register_profile("ci-views", parent=settings.get_profile("ci"), max_examples=10)
settings.register_profile("deep-views", parent=settings.get_profile("deep"), max_examples=500)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
