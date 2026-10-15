"""The writer for every CSV the portal hands out, so user text in it can't run as a spreadsheet formula.

A spreadsheet treats a cell that starts with =, +, -, @, a tab or a carriage return as a formula (CSV injection), so
a team named =HYPERLINK(...) would run in whoever opens the export. Such text cells get a leading single quote (the
OWASP rule); numbers are written as numbers. test_property_coverage fails on a CSV writer that isn't this one.
"""

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
