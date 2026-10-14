"""Names a framework reads that vulture can't see used: test_no_unused_python_code scans this file as a user.

Prefer a decorator or name pattern in that test's IGNORE_DECORATORS / IGNORE_NAMES; list single names here only
when no pattern fits.
"""

from vulture.whitelist_utils import Whitelist

_ = Whitelist()

# gunicorn reads its settings from web/gunicorn.conf.py by name
_.bind
_.workers
_.preload_app
_.accesslog
_.errorlog
_.loglevel
_.access_log_format
_.when_ready

# Django admin reads these off the AdminSite (web/core/admin.py)
_.site_header
_.site_title
_.index_title
_.site_url

# Setting request.session.modified tells Django to save the session
_.modified
