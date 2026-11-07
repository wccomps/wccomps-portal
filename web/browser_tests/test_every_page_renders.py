"""The browser walk (every_page) renders every page the view property rows reach as some role, so the contrast and
phone width tests check it. Not a browser test: it runs with the rest of CI."""

from urllib.parse import urlsplit

import pytest
from django.http import HttpResponseBase

from core.tests.guards import fail_with
from core.tests.views_harness import build_world, client_for, offline

from .every_page import UNRENDERED, pages, rendered_as

pytestmark = pytest.mark.django_db


def _answer(response: HttpResponseBase) -> str:
    if location := response.get("Location"):
        held = urlsplit(location)._replace(query="").geturl()
    else:
        held = "streamed" if response.streaming else response.get("Content-Type", "").split(";")[0]
    return f"{response.status_code} {held}"


def test_every_page_is_rendered_as_some_role() -> None:
    found = {}
    with offline():
        world = build_world()
        for case, name, path in pages(world):
            if rendered_as(world, case, path):
                continue
            responses = [client_for(world, role).get(path) for role in case.allowed]
            # A 405 to a GET is a view that takes only POST: no page
            if any(response.status_code != 405 for response in responses):
                found[name] = ", ".join(sorted({_answer(response) for response in responses}))
    fail_with("Pages no role gets as 200 HTML, so the browser walk checks none of them", found, UNRENDERED)
