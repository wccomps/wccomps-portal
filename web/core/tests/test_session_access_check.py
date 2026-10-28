"""The portal's Authentik token must be able to see sessions, or ending a team's sessions does nothing."""

from unittest.mock import patch

from core.admin_views.readiness import ALL_CHECKS, _check_session_access
from core.authentik_manager import SESSIONS_HIDDEN


def test_passes_when_the_token_sees_sessions():
    with patch("core.admin_views.readiness.AuthentikManager") as manager:
        manager.return_value.__enter__.return_value.sees_any_sessions.return_value = True
        severity, _, _ = _check_session_access()
    assert severity == "pass"


def test_fails_when_the_token_sees_no_sessions():
    with patch("core.admin_views.readiness.AuthentikManager") as manager:
        manager.return_value.__enter__.return_value.sees_any_sessions.return_value = False
        severity, detail, _ = _check_session_access()
    assert severity == "fail"
    assert detail.startswith(SESSIONS_HIDDEN)


def test_readiness_runs_the_check():
    assert any(fn is _check_session_access for _, fn in ALL_CHECKS)
