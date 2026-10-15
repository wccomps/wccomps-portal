"""Static checks that keep table header rows consistent (each header's text lines up with the others).

Column headers go through c-table_header, whose <span>/<a> Django admin's CSS pads alike; a hand-written
<th> with bare text sits higher. The one exception is the select-all checkbox, which uses the project's
checkbox column classes so it lines up with the row checkboxes.
"""

import re

import pytest

from core.tests.guards import is_standalone_document, template_name, tracked_templates

# Not styled by the portal's admin CSS, with the reason. Standalone documents (guards.is_standalone_document)
# carry their own styles and are left out too.
EXCLUDED = {
    "cotton/table_header.html": "the component itself",
}

HEADER_ROWS = re.compile(r'<thead\b.*?</thead>|<c-slot name="headers">.*?</c-slot>', re.S)
HAND_WRITTEN_TH = re.compile(r"<th\b[^>]*>.*?</th>", re.S)
SELECT_ALL = re.compile(r'type="checkbox"')
ROW_CHECKBOX = re.compile(r'<input\b[^>]*type="checkbox"[^>]*@change="toggleItem"', re.S)


def _templates() -> list[tuple[str, str]]:
    return [
        (template_name(path), path.read_text())
        for path in tracked_templates()
        if not is_standalone_document(path) and template_name(path) not in EXCLUDED
    ]


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def test_column_headers_use_the_component() -> None:
    offenders = []
    for rel, text in _templates():
        for rows in HEADER_ROWS.finditer(text):
            for th in HAND_WRITTEN_TH.finditer(rows.group(0)):
                cell = th.group(0)
                where = f"{rel}:{_line(text, rows.start() + th.start())}"
                if not SELECT_ALL.search(cell):
                    offenders.append(f'{where}: use <c-table_header> (sortable="false" if not sortable)')
                elif 'class="action-checkbox-column"' not in cell:
                    offenders.append(f'{where}: the select-all <th> needs class="action-checkbox-column"')
    if offenders:
        pytest.fail("Hand-written table headers:\n" + "\n".join(offenders))


def test_row_checkboxes_share_the_select_all_column() -> None:
    offenders = []
    for rel, text in _templates():
        for checkbox in ROW_CHECKBOX.finditer(text):
            cell_start = text.rfind("<td", 0, checkbox.start())
            cell_tag = text[cell_start : text.index(">", cell_start) + 1]
            if 'class="action-checkbox"' not in cell_tag:
                offenders.append(f"{rel}:{_line(text, cell_start)}")
    if offenders:
        pytest.fail('Row checkbox cells need class="action-checkbox":\n' + "\n".join(offenders))


def test_current_sort_is_the_context_value() -> None:
    """current_sort="sort_by" passes the string "sort_by", so the sort arrow never shows."""
    offenders = [
        f"{rel}:{_line(text, m.start())}: {m.group(0)}"
        for rel, text in _templates()
        for m in re.finditer(r'(?<![:\w])current_sort="[^"]*"', text)
        if "{{" not in m.group(0) and rel != "cotton/README.md"
    ]
    if offenders:
        pytest.fail("current_sort must be a template value like {{ sort_by }}:\n" + "\n".join(offenders))
