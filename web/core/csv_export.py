"""Every CSV export's writer: a text cell that starts like a formula (=, +, -, @, tab, CR) gets a leading quote, the
OWASP rule against CSV injection; test_property_coverage enforces its use."""

import csv
from collections.abc import Iterable
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import SupportsWrite

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: object) -> object:
    """The value as a cell a spreadsheet won't run: text that starts like a formula is prefixed with '."""
    if value is None or isinstance(value, (int, float, Decimal)):  # bool is an int
        return value
    text = str(value)
    return f"'{text}" if _starts_a_formula(text) else text


def unquote_cell(text: str) -> str:
    """A cell as it was before safe_cell quoted it, for reading an export back in."""
    return text[1:] if text.startswith("'") and _starts_a_formula(text) else text


def _starts_a_formula(text: str) -> bool:
    # Quotes already in front count too, so unquote_cell takes off exactly the one safe_cell added
    return text.lstrip("'").startswith(FORMULA_STARTS)


class CsvWriter:
    """csv.writer that writes every cell through safe_cell."""

    def __init__(self, out: SupportsWrite[str]) -> None:
        self._writer = csv.writer(out)

    def writerow(self, row: Iterable[object]) -> None:
        self._writer.writerow([safe_cell(value) for value in row])

    def writerows(self, rows: Iterable[Iterable[object]]) -> None:
        for row in rows:
            self.writerow(row)


def csv_writer(out: SupportsWrite[str]) -> CsvWriter:
    return CsvWriter(out)
