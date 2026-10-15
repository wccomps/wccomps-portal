"""Hypothesis profiles for every test under the repo, chosen by HYPOTHESIS_PROFILE (default "ci")."""

import os

from hypothesis import HealthCheck, settings

# Property tests take pytest fixtures once per test and roll each example back themselves
# (core.tests.strategies.rolled_back), so the function-scoped fixture check doesn't apply. Hostile inputs are
# slow and large on purpose (5000-character strings, files), so the speed and size checks don't either.
_SUPPRESSED = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large]

# ci: the same examples every run, few enough that the whole suite stays fast
settings.register_profile(
    "ci", max_examples=25, derandomize=True, deadline=None, database=None, suppress_health_check=_SUPPRESSED
)
# deep: many random examples, for a search before a release or after changing input handling
settings.register_profile("deep", max_examples=2000, deadline=None, suppress_health_check=_SUPPRESSED)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
