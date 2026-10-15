"""The web server's master process loads what a worker's first page would otherwise load, before gunicorn binds
and forks, so workers start warm."""

import importlib
import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from django.template import engines

WEB = Path(__file__).resolve().parents[2]
REPO = WEB.parent


@pytest.fixture(autouse=True)
def _no_freeze_in_the_test_process():
    """gc.freeze() would keep everything the test process has allocated so far for the rest of the run."""
    with patch("portal.warmup.gc.freeze") as freeze:
        yield freeze


def _cached_loader():
    return engines["django"].engine.template_loaders[0]


def test_warming_compiles_every_template_without_touching_the_database():
    from portal.warmup import warm

    loader = _cached_loader()
    loader.reset()

    # Not a django_db test: a query here (in the master, before forking) would fail it
    warm()

    project_templates = {path.relative_to(WEB / "templates").as_posix() for path in (WEB / "templates").rglob("*.html")}
    assert project_templates <= set(loader.get_template_cache)
    assert {"admin/competition.html", "cotton/module.html", "admin/change_list.html"} <= set(loader.get_template_cache)


def test_a_template_that_fails_to_compile_is_logged_and_warming_goes_on(tmp_path, settings, caplog):
    from portal.warmup import warm

    (tmp_path / "broken.html").write_text("{% if %}")
    settings.TEMPLATES = [{**settings.TEMPLATES[0], "DIRS": [tmp_path, *settings.TEMPLATES[0]["DIRS"]]}]

    warm()

    assert "Could not warm template broken.html" in caplog.text
    assert "admin/competition.html" in _cached_loader().get_template_cache


def test_warming_freezes_what_it_loaded_so_the_workers_share_it(_no_freeze_in_the_test_process):
    from portal.warmup import warm

    warm()

    _no_freeze_in_the_test_process.assert_called_once_with()


def test_the_wsgi_module_warms_so_preload_runs_it_before_gunicorn_binds(monkeypatch):
    monkeypatch.setenv("DJANGO_SECRET_KEY", "test-only-key")
    sys.modules.pop("portal.wsgi", None)

    with patch("portal.warmup.warm") as warm:
        importlib.import_module("portal.wsgi")

    warm.assert_called_once_with()
    config = runpy.run_path(str(WEB / "gunicorn.conf.py"))
    assert config["preload_app"] is True
    assert "when_ready" not in config
    assert 'CMD ["gunicorn", "portal.wsgi:application"]' in (REPO / "Dockerfile").read_text()


PROBE = """
import sys
import django
django.setup()
from django.core.management import call_command
call_command("check")
print("LOADED:" + ",".join(sorted(m for m in sys.modules if m in ("portal.warmup", "portal.wsgi"))))
"""


def test_django_setup_and_management_commands_never_warm():
    """The bot, migrations and manage.py set Django up without the web server's module, so they stay cold."""
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "portal.settings"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE], cwd=WEB, env=env, capture_output=True, text=True, check=True, timeout=120
    )

    assert next(line for line in result.stdout.splitlines() if line.startswith("LOADED:")) == "LOADED:"
