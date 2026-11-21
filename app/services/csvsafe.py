"""CSV cells that are safe to open in Excel / Google Sheets.

A cell that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return is run
as a formula by spreadsheet apps. Names, addresses and reasons are typed by
employees, so such text gets a leading apostrophe (shown by Excel as plain text).
Real numbers (``-500``, ``+91``-less amounts) are left alone.
"""

from __future__ import annotations

FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def neutralize(value: object) -> object:
    if not isinstance(value, str) or not value.startswith(FORMULA_STARTS):
        return value
    try:
        float(value)  # a plain number such as "-500" or "+5.5" is data, not a formula
        return value
    except ValueError:
        return "'" + value


def csv_cell(value: object) -> str:
    """One CSV field: formula-safe, and quoted when it contains a comma, quote or newline."""
    text = "" if value is None else str(neutralize(value) if isinstance(value, str) else value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text
