"""Starting Django (the bot does, through the admin's imports) must not load the scoring views or WeasyPrint."""

import os
import subprocess
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[2]

PROBE = """
import sys
import django
django.setup()
loaded = sorted(m for m in sys.modules if m.split(".")[0] == "weasyprint" or m.startswith("scoring.views"))
print("LOADED:" + ",".join(loaded))
"""


def test_django_setup_loads_neither_weasyprint_nor_the_scoring_views():
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "portal.settings"}

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE], cwd=WEB, env=env, capture_output=True, text=True, check=True, timeout=120
    )

    loaded = next(line for line in result.stdout.splitlines() if line.startswith("LOADED:"))
    assert loaded == "LOADED:"
