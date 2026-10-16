"""A component's {{ attrs }} carries every attribute it doesn't declare in <c-vars>, and django-cotton writes a
dynamic one (:name="value") unescaped. So a page handing a component request text (a search box, a list filter)
put that text in the page as raw markup. Every dynamic attribute a template passes to a component that writes
{{ attrs }} must be one the component declares, or an exception here with why its value can't carry markup.
"""

import re
from functools import cache

from core.tests.guards import TEMPLATES_DIR, fail_with, template_name, tracked_templates

COTTON_DIR = TEMPLATES_DIR / "cotton"
COMPONENT_TAG = re.compile(r"<c-([\w.-]+)\b(.*?)/?>", re.DOTALL)
DYNAMIC_ATTR = re.compile(r"\s:([\w-]+)=")
VARS_TAG = re.compile(r"<c-vars\b(.*?)/>", re.DOTALL)
VAR_NAME = re.compile(r"([\w:-]+)(?==|\s|$)")

# "where:component:attribute" -> why its value can't carry markup
EXCEPTIONS = {
    "admin/team_detail.html:button:disabled": "a boolean the view sets",
    "orange_team/check_form.html:button:data-index": "the rubric row's number from the template's loop",
}


@cache
def components() -> dict[str, tuple[bool, frozenset[str]]]:
    """Each component: whether it writes {{ attrs }}, and the names it declares in <c-vars>."""
    found = {}
    for path in tracked_templates():
        if not path.is_relative_to(COTTON_DIR):
            continue
        source = path.read_text(encoding="utf-8")
        declared = VARS_TAG.search(source)
        names = {n.lstrip(":").replace("-", "_") for n in VAR_NAME.findall(declared.group(1))} if declared else set()
        name = path.relative_to(COTTON_DIR).with_suffix("").as_posix().replace("/", ".")
        found[name] = ("{{ attrs }}" in source, frozenset(names))
    return found


def undeclared_dynamic_attrs(where: str, source: str) -> dict[str, str]:
    found = {}
    for tag in COMPONENT_TAG.finditer(source):
        component = tag.group(1)
        writes_attrs, declared = components().get(component, (False, frozenset()))
        if not writes_attrs:
            continue
        for attr in DYNAMIC_ATTR.findall(tag.group(2)):
            if attr.replace("-", "_") not in declared:
                found[f"{where}:{component}:{attr}"] = "passed with : but not in its <c-vars>"
    return found


def test_a_component_declares_every_dynamic_attribute_it_is_passed() -> None:
    found = {
        key: why
        for path in tracked_templates()
        for key, why in undeclared_dynamic_attrs(template_name(path), path.read_text(encoding="utf-8")).items()
    }
    fail_with("Dynamic attributes a component writes unescaped into {{ attrs }}", found, EXCEPTIONS)


def test_the_guard_finds_an_undeclared_dynamic_attribute() -> None:
    source = '<c-ticket_list_table :page_obj="page_obj" :not_declared="search" title="{{ search }}" />'
    assert list(undeclared_dynamic_attrs("page.html", source)) == ["page.html:ticket_list_table:not_declared"]
