"""The competition pages use only classes the portal's or Django admin's stylesheets define (no Bootstrap is loaded)."""

import re
from pathlib import Path

import django
import pytest

WEB = Path(__file__).resolve().parents[2]
ADMIN_CSS = Path(django.__file__).parent / "contrib/admin/static/admin/css"
TEMPLATES = [
    "admin/competition.html",
    "admin/competition_setup.html",
    "admin/includes/readiness_check.html",
    *sorted(p.relative_to(WEB / "templates").as_posix() for p in (WEB / "templates/admin/competition").glob("*.html")),
]
# Hooks with no style of their own: Django admin's row striping classes and its field-<name> cell classes
HOOKS = {"row1", "row2"}


def _defined():
    css = (WEB / "static/css/app.css").read_text()
    css += "".join(path.read_text() for path in ADMIN_CSS.rglob("*.css"))
    return set(re.findall(r"\.([A-Za-z_][\w-]*)", css))


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_class_is_defined(template):
    html = (WEB / "templates" / template).read_text()
    defined = _defined()

    used = set()
    for value in re.findall(r'(?<![:\w-])class="([^"]*)"', html):
        used |= set(re.sub(r"\{[{%].*?[%}]\}", " ", value).split())

    assert {name for name in used - defined - HOOKS if not name.startswith("field-")} == set()
