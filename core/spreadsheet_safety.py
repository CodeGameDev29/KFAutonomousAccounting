"""Keep document-supplied text from executing as a formula in an exported file.

Vendor names, bank descriptors, notes and invoice numbers come from documents
and statements other people wrote. A spreadsheet application treats a cell that
starts with ``=``, ``+``, ``-`` or ``@`` as a formula, so an invoice whose vendor
reads ``=HYPERLINK(...)`` would otherwise arrive, live, in the workbook an
accountant opens. Exports are where that text leaves this application, so this is
where it is made inert.

Two formats, two mechanisms:

* **XLSX** has typed cells, so nothing is rewritten: a cell openpyxl inferred to
  be a formula is stored as a string instead, and the text stays exactly what
  the document said. The exporters write no formulas of their own (totals are
  computed in Python, links are ``Hyperlink`` objects), so every formula-typed
  cell at save time came from data.
* **CSV** has no types, so the conventional guard applies: a leading apostrophe,
  which spreadsheet applications hide and treat as "this is text". It is applied
  only to text. A value that parses as a number (``-67.20``) is a number and is
  left alone — an amount must never be altered on its way out.
"""

from __future__ import annotations

from typing import Any, Iterable

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def csv_safe(value: Any) -> Any:
    """Return ``value`` made inert for a CSV cell; non-strings pass through."""
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(_FORMULA_LEADERS) and not _is_number(value.strip()):
        return "'" + value
    return value


class SafeCsvWriter:
    """A ``csv.writer`` wrapper whose rows go through :func:`csv_safe`."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._writer.writerow([csv_safe(cell) for cell in row])

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def neutralize_workbook_formulas(workbook: Any) -> int:
    """Store every formula-typed cell of an openpyxl workbook as plain text.

    Call immediately before ``workbook.save``. Returns how many cells were
    changed, which is zero for any export built from ordinary data.
    """
    changed = 0
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
                    changed += 1
    return changed
