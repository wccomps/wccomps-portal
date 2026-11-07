"""Every page's text reads at WCAG AA contrast against what it is drawn on, in the light and the dark theme."""

from collections import defaultdict
from contextlib import closing

import pytest

from .every_page import open_pages

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]

# Each visible element holding text of its own (or a button's value) whose color, composited over the backgrounds
# behind it, reads below 4.5:1, or 3:1 for large text (24px, or 18.66px bold). A disabled control is exempt.
LOW_CONTRAST = """() => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    // The canvas parses any CSS color syntax a computed style can hold
    const rgba = (color) => {
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillStyle = "rgba(0, 0, 0, 0)";
        ctx.fillStyle = color;
        ctx.fillRect(0, 0, 1, 1);
        const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
        return { r, g, b, a: a / 255 };
    };
    const over = (top, bottom) => ({
        r: top.r * top.a + bottom.r * (1 - top.a),
        g: top.g * top.a + bottom.g * (1 - top.a),
        b: top.b * top.a + bottom.b * (1 - top.a),
        a: 1,
    });
    const channel = (v) => ((v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    const luminance = ({ r, g, b }) => 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    const ratio = (x, y) => {
        const [hi, lo] = [luminance(x), luminance(y)].sort((p, q) => q - p);
        return (hi + 0.05) / (lo + 0.05);
    };
    const css = (c) => `rgb(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)})`;
    const name = (el) => el.tagName.toLowerCase() + [...el.classList].slice(0, 2).map((c) => "." + c).join("");
    const describe = (el) => {
        const parts = [];
        for (let e = el; e && e !== document.body && parts.length < 3; e = e.parentElement) parts.unshift(name(e));
        return parts.join(" > ");
    };
    const background = (el) => {
        const layers = [];
        for (let e = el; e; e = e.parentElement) {
            const color = rgba(getComputedStyle(e).backgroundColor);
            if (color.a > 0) {
                layers.push(color);
                if (color.a === 1) break;
            }
        }
        return layers.reverse().reduce((base, layer) => over(layer, base), { r: 255, g: 255, b: 255, a: 1 });
    };
    const found = new Map();
    const buttons = 'input[type="submit"], input[type="button"], input[type="reset"]';
    for (const el of document.body.querySelectorAll("*")) {
        if (["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "OPTION"].includes(el.tagName)) continue;
        const text = el.matches(buttons)
            ? el.value.trim()
            : [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join(" ").trim();
        if (!text || el.closest(':disabled, [aria-disabled="true"]')) continue;
        const style = getComputedStyle(el);
        const box = el.getBoundingClientRect();
        if (style.visibility !== "visible" || box.width <= 1 || box.height <= 1) continue;
        let opacity = 1;
        for (let e = el; e; e = e.parentElement) opacity *= parseFloat(getComputedStyle(e).opacity);
        if (opacity === 0) continue;
        const bg = background(el);
        const fg = rgba(style.color);
        const measured = ratio(over({ ...fg, a: fg.a * opacity }, bg), bg);
        const size = parseFloat(style.fontSize);
        const need = size >= 24 || (size >= 18.66 && parseInt(style.fontWeight, 10) >= 700) ? 3 : 4.5;
        if (measured >= need) continue;
        const key = `${describe(el)}|${style.color}|${css(bg)}`;
        if (!found.has(key))
            found.set(key, { el: describe(el), text: text.slice(0, 40), fg: style.color, bg: css(bg),
                             ratio: Math.round(measured * 100) / 100, need });
    }
    return [...found.values()];
}"""


def test_text_reads_at_aa_contrast_on_every_page(live_server, pw_browser):
    found: defaultdict[str, list[str]] = defaultdict(list)
    with closing(open_pages(pw_browser, live_server, {"width": 1280, "height": 800}, ("light", "dark"))) as visits:
        for visit, scheme, page in visits:
            for low in page.evaluate(LOW_CONTRAST):
                pair = f"{low['el']}: {low['fg']} on {low['bg']} = {low['ratio']}, needs {low['need']}"
                found[pair].append(f"{visit.name} as {visit.role} ({scheme}) {low['text']!r}")

    report = [
        f"{pair}\n    " + "\n    ".join(pages[:4]) + f"\n    ({len(pages)} in all)" for pair, pages in found.items()
    ]
    assert not found, "Text below WCAG AA contrast:\n" + "\n".join(report)
