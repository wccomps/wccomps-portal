"""Gunicorn's master loads what a worker's first page would otherwise load, so forked workers start warm."""

import runpy
from pathlib import Path

from django.template import engines

WEB = Path(__file__).resolve().parents[2]


def test_the_master_compiles_every_template_without_touching_the_database():
    loader = engines["django"].engine.template_loaders[0]
    loader.reset()

    # Not a django_db test: a query here (in the master, before forking) would fail it
    runpy.run_path(str(WEB / "gunicorn.conf.py"))["when_ready"](None)

    project_templates = {path.relative_to(WEB / "templates").as_posix() for path in (WEB / "templates").rglob("*.html")}
    assert project_templates <= set(loader.get_template_cache)
    assert {"admin/competition.html", "cotton/module.html", "admin/change_list.html"} <= set(loader.get_template_cache)


def test_a_template_that_fails_to_compile_is_logged_and_the_master_still_starts(tmp_path, settings, caplog):
    (tmp_path / "broken.html").write_text("{% if %}")
    settings.TEMPLATES = [{**settings.TEMPLATES[0], "DIRS": [tmp_path, *settings.TEMPLATES[0]["DIRS"]]}]

    runpy.run_path(str(WEB / "gunicorn.conf.py"))["when_ready"](None)

    assert "Could not warm template broken.html" in caplog.text
