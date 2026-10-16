"""Static checks that keep table header rows consistent (each header's text lines up with the others).

Column headers go through c-table_header, whose <span>/<a> Django admin's CSS pads alike; a hand-written
<th> with bare text sits higher. The one exception is the select-all checkbox, which uses the project's
checkbox column classes so it lines up with the row checkboxes.
"""

from core.tests.guards import Tag, fail_with, portal_templates, tags, template_name, with_open_elements


def in_header_row(open_tags: list[Tag]) -> bool:
    return any(t.name == "thead" or (t.name == "c-slot" and t.attrs.get("name") == "headers") for t in open_tags)


def test_column_headers_use_the_component() -> None:
    found = {}
    for path in portal_templates():
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
    for path in portal_templates():
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
        for path in portal_templates()
        for tag in tags(path)
        if tag.name != "c-vars" and "{{" not in tag.attrs.get("current_sort", "{{")
    }
    fail_with("current_sort must be a template value like {{ sort_by }}", found, {})
