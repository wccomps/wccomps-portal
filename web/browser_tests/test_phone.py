"""On a phone every page fits the width (a wide table scrolls inside its own box), and every control can be tapped:
24px across, or 24px clear of the next one (WCAG 2.5.8)."""

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


# Each control under 24px across whose 24px circle reaches another control or another small control's circle. WCAG
# exempts a link inside a line of text and the browser's own unstyled checkbox. This test also skips the new ticket
# toast, which shows for a few seconds over whatever has scrolled beneath it.
SMALL_TARGETS = """() => {
    const shown = (el) => {
        const style = getComputedStyle(el);
        const box = el.getBoundingClientRect();
        return style.visibility === "visible" && style.display !== "none" && box.width > 1 && box.height > 1;
    };
    const inText = (el) => getComputedStyle(el).display === "inline"
        && el.parentElement.textContent.trim().length > el.textContent.trim().length + 2;
    const browserSized = (el, box) => el.matches("input[type=checkbox], input[type=radio]")
        && Math.round(box.width) === 13 && Math.round(box.height) === 13;
    const selector = "a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [tabindex]";
    const targets = [...document.querySelectorAll(selector)]
        .filter((el) => el.tabIndex >= 0 && shown(el) && !el.closest(".ticket-toast"))
        .map((el) => ({ el, box: el.getBoundingClientRect() }));
    const small = targets.filter(({ el, box }) =>
        (box.width < 24 || box.height < 24) && !inText(el) && !browserSized(el, box));
    const center = (box) => [box.left + box.width / 2, box.top + box.height / 2];
    // From a point to the nearest edge of a box (0 inside it)
    const gap = ([x, y], box) =>
        Math.hypot(Math.max(box.left - x, 0, x - box.right), Math.max(box.top - y, 0, y - box.bottom));
    const name = (el) => el.tagName.toLowerCase() + [...el.classList].slice(0, 2).map((c) => "." + c).join("");
    return small
        .filter((t) => {
            const c = center(t.box);
            return targets.some((o) => o !== t && !o.el.contains(t.el) && !t.el.contains(o.el) && gap(c, o.box) < 12)
                || small.some((o) => o !== t && Math.hypot(center(o.box)[0] - c[0], center(o.box)[1] - c[1]) < 24);
        })
        .map(({ el, box }) => {
            const text = (el.textContent || el.value || "").trim().slice(0, 30);
            const size = `${Math.round(box.width)}x${Math.round(box.height)}`;
            return `${name(el.parentElement)} > ${name(el)} ${size} "${text}"`;
        });
}"""


def test_every_page_fits_a_phone_and_its_controls_can_be_tapped(live_server, pw_browser):
    wide, cramped = [], []
    with closing(open_pages(pw_browser, live_server, PHONE)) as visits:
        for visit, _, page in visits:
            if overflow := page.evaluate(OVERFLOW):
                wide.append(
                    f"{visit.name} as {visit.role}: {overflow['width']}px wide, " + "; ".join(overflow["culprits"])
                )
            if small := page.evaluate(SMALL_TARGETS):
                cramped.append(f"{visit.name} as {visit.role}: " + "; ".join(small))

    assert not wide, f"Pages wider than a {PHONE['width']}px phone:\n" + "\n".join(wide)
    assert not cramped, "Controls under 24px and within 24px of another (WCAG 2.5.8):\n" + "\n".join(cramped)
