import re
from html import unescape


def field_error(html: str, field_id: str) -> str | None:
    """The error shown in a field's own row, under the id its widget's aria-describedby names."""
    match = re.search(rf'<p class="errornote" id="{field_id}_error">([^<]*)</p>', html)
    return unescape(match.group(1)) if match else None
