"""Static checks that keep table header rows consistent (each header's text lines up with the others).

Column headers go through c-table_header, whose <span>/<a> Django admin's CSS pads alike; a hand-written
<th> with bare text sits higher. The one exception is the select-all checkbox, which uses the project's
checkbox column classes so it lines up with the row checkboxes.
"""

from collections.abc import Iterator
from pathlib import Path

from core.tests.guards import Tag, elements, fail_with, is_standalone_document, tags, template_name, tracked_templates

# Not styled by the portal's admin CSS, with the reason. Standalone documents (guards.is_standalone_document)
# carry their own styles and are left out too.
EXCLUDED = {
    "cotton/table_header.html": "the component itself",
}
VOID = {"input", "br", "img", "hr", "meta", "link", "source", "col", "wbr", "area", "base", "embed", "track"}


def _templates() -> list[Path]:
    return [
        path for path in tracked_templates() if not is_standalone_document(path) and template_name(path) not in EXCLUDED
    ]


def with_open_elements(path: Path) -> Iterator[tuple[Tag, list[Tag]]]:
    """Each start tag with the elements open around it, outermost first."""
    open_tags: list[Tag] = []
    for tag in elements(path):
        if tag.closing:
            names = [t.name for t in open_tags]
            if tag.name in names:
                del open_tags[len(names) - 1 - names[::-1].index(tag.name) :]
            continue
        yield tag, open_tags
        if tag.name not in VOID:
            open_tags.append(tag)


def in_header_row(open_tags: list[Tag]) -> bool:
    return any(t.name == "thead" or (t.name == "c-slot" and t.attrs.get("name") == "headers") for t in open_tags)


def test_column_headers_use_the_component() -> None:
    found = {}
    for path in _templates():
        header_cells: dict[int, tuple[Tag, bool]] = {}
        for tag, open_tags in with_open_elements(path):
            if tag.name == "th" and in_header_row(open_tags):
                header_cells[id(tag)] = (tag, False)
            cell = next((t for t in reversed(open_tags) if t.name == "th"), None)
            if cell is not None and tag.name == "input" and tag.attrs.get("type") == "checkbox":
                header_cells[id(cell)] = (cell, True)
        for th, select_all in header_cells.values():
            where = f"{template_name(path)}:{th.line}"
            if not select_all:
                found[where] = 'use <c-table_header> (sortable="false" if not sortable)'
            elif "action-checkbox-column" not in th.attrs.get("class", "").split():
                found[where] = 'the select-all <th> needs class="action-checkbox-column"'
    fail_with("Hand-written table headers", found, {})


def test_row_checkboxes_share_the_select_all_column() -> None:
    found = {}
    for path in _templates():
        for tag, open_tags in with_open_elements(path):
            if tag.name == "input" and tag.attrs.get("type") == "checkbox" and tag.attrs.get("@change") == "toggleItem":
                cell = next((t for t in reversed(open_tags) if t.name == "td"), None)
                if cell is None or "action-checkbox" not in cell.attrs.get("class", "").split():
                    found[f"{template_name(path)}:{tag.line}"] = 'the row checkbox cell needs class="action-checkbox"'
    fail_with("Row checkbox cells", found, {})


def test_current_sort_is_the_context_value() -> None:
    """current_sort="sort_by" passes the string "sort_by", so the sort arrow never shows."""
    found = {
        f"{template_name(path)}:{tag.line}": f'current_sort="{tag.attrs["current_sort"]}"'
        for path in _templates()
        for tag in tags(path)
        if "{{" not in tag.attrs.get("current_sort", "{{")
    }
    fail_with("current_sort must be a template value like {{ sort_by }}", found, {})
