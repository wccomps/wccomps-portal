"""Every page fits a phone's width: nothing makes the page scroll sideways. A wide table scrolls inside its own box."""

from contextlib import closing

import pytest

from .every_page import open_pages

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

PHONE = {"width": 390, "height": 844}

# The page's width when it is wider than the viewport, and the innermost elements that reach past the viewport
# outside a box that scrolls or clips them: those are what to fix.
OVERFLOW = """() => {
    const viewport = document.documentElement.clientWidth;
    const width = document.documentElement.scrollWidth;
    if (width <= viewport) return null;
    const contained = (el) => {
        for (let e = el.parentElement; e && e !== document.body; e = e.parentElement)
            if (getComputedStyle(e).overflowX !== "visible") return true;
        return false;
    };
    const past = (el) => el.getBoundingClientRect().right > viewport + 1;
    const name = (el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : "")
        + [...el.classList].slice(0, 2).map((c) => "." + c).join("");
    const culprits = [...document.body.querySelectorAll("*")]
        .filter((el) => el.getBoundingClientRect().width > 0 && past(el) && !contained(el))
        .filter((el) => [...el.children].every((child) => !past(child) || contained(child)))
        .slice(0, 3)
        .map((el) => `${el.parentElement ? name(el.parentElement) + " > " : ""}${name(el)}`);
    return { width, culprits };
}"""


def test_every_page_fits_a_phone(live_server, pw_browser):
    found = []
    with closing(open_pages(pw_browser, live_server, PHONE)) as visits:
        for visit, _, page in visits:
            if overflow := page.evaluate(OVERFLOW):
                found.append(
                    f"{visit.name} as {visit.role}: {overflow['width']}px wide, " + "; ".join(overflow["culprits"])
                )

    assert not found, f"Pages wider than a {PHONE['width']}px phone:\n" + "\n".join(found)
