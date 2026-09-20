"""Parse ground-truth XLSX files into normalized GroundTruthRow records.

This reader supports **a** monthly workbook layout, not one particular file:

  1. a sheet holding one or more currency sections stacked in a single tab: a
     header row whose amount column names the currency, its data rows, then
     (optionally) a blank or SUM row and a second header for the next currency;
  2. a card sheet, whose name says so: optional metadata rows, then a header
     carrying both a transaction and a posting date, then data;
  3. any other tab is reference material and is skipped.

Which tab is which is decided by :data:`SHEET_KINDS`, and which tabs are skipped
outright by :data:`DEFAULT_SKIP_SHEETS`. Both are documented tables to edit or
override per call, as is the set of currencies a section header may name.

A hand-kept workbook drifts, so the reader is deliberately tolerant of the
variations that show up month to month:
  - header in row 1 with no metadata prefix, or a metadata timestamp in row 1
    and the header three rows down
  - column name spelled "Transaction Amount CAD" or "Transaction Amount (CAD)"
  - a SUM row between two currency sections, or none

Usage:
    from scripts.qa.xlsx_parser import parse_xlsx
    rows = parse_xlsx(Path("data/books/2026/ledger-2026-01.xlsx"))

``tests/fixtures/ledger-2026-01.xlsx`` is a synthetic workbook in this shape.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl

logger = logging.getLogger(__name__)


@dataclass
class GroundTruthRow:
    """A single normalized transaction from the ground-truth XLSX."""
    account: str  # an account key, as in config/accounts.yaml
    transaction_type: str  # "DEBIT" or "CREDIT"
    date_posted: date
    amount: Decimal  # signed (negative for debits)
    description: str
    category: str
    invoice: str  # Invoice/Cheque column value
    note: str
    sheet_name: str  # source sheet for debugging
    row_number: int  # source row for debugging


# ── Category normalization ──────────────────────────────────────────
# A hand-kept workbook spells a category however it was spelled that month, so
# the comparison needs a small table mapping those spellings onto the app's
# canonical category names (config/ised_categories.py).
#
# The entries below are EXAMPLES of the three kinds of drift a workbook makes
# you absorb; replace them with your own workbook's spellings. Anything not in
# this table is passed through unchanged, so an empty table is valid and costs
# only the mismatches it would have absorbed.
CATEGORY_NORMALIZATION = {
    # 1. A different name for the same thing.
    "Bank fees": "Interest and bank charges",
    # 2. A casing variant.
    "office supplies": "Office supplies",
    # 3. A singular/plural variant.
    "Office supply": "Office supplies",
}


def normalize_category(raw: str) -> str:
    """Normalize an XLSX category to the app's canonical form."""
    raw = raw.strip()
    return CATEGORY_NORMALIZATION.get(raw, raw)


# ── Sheet selection ─────────────────────────────────────────────────
# Sheets that hold no transaction data at all. A hand-kept workbook usually
# carries a few auxiliary tabs — an index, a running total, notes, an export
# pasted in from somewhere else — alongside the account sheets. These are
# substring matches against the lower-cased sheet name. Edit the list, or pass
# `skip_sheets=` to `parse_xlsx`, to match your own workbook.
DEFAULT_SKIP_SHEETS: tuple[str, ...] = (
    "index", "contents", "summary", "notes", "total", "orders",
)

# Which reader a sheet needs, decided from its name: (kind, substrings), tried
# in order against the lower-cased sheet name. "card" comes first because a
# card tab's name may also carry a currency. A sheet that matches nothing here
# is skipped with a debug line rather than guessed at. Pass `sheet_kinds=` to
# `parse_xlsx` for a workbook that names its tabs some other way.
SHEET_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("card", ("credit card", "creditcard", "credit_card", "card")),
    ("currency_sections", ("cad", "usd", "cny", "eur", "gbp",
                           "currency", "chequing", "checking", "bank")),
)

#: Currency codes a section header may name in its amount column, and the one
#: assumed when a header names none. Both are here rather than inline so a
#: workbook kept in other currencies needs no code change.
SECTION_CURRENCIES: tuple[str, ...] = ("CAD", "USD", "CNY", "EUR", "GBP")
DEFAULT_SECTION_CURRENCY: str = "CAD"


def sheet_kind(
    sheet_name: str,
    sheet_kinds: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
) -> str | None:
    """Which reader a sheet needs: ``"card"``, ``"currency_sections"``, or None."""
    lowered = sheet_name.lower()
    for kind, needles in (sheet_kinds or SHEET_KINDS):
        if any(n in lowered for n in needles):
            return kind
    return None


# ── Date parsing ────────────────────────────────────────────────────

def _parse_date_value(val) -> date | None:
    """Parse a date from XLSX cell value.

    XLSX dates are stored as:
      - int/float YYYYMMDD (e.g. 20000102.0 for 2000-01-02)
      - datetime objects
      - strings ("20000102")
    """
    if val is None:
        return None

    # Numeric YYYYMMDD (most common in these files)
    if isinstance(val, (int, float)):
        try:
            s = str(int(val))
            if len(s) == 8:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except (ValueError, OverflowError):
            pass
        return None

    # datetime object
    if hasattr(val, "date"):
        return val.date() if callable(getattr(val, "date")) else val.date

    # String
    if isinstance(val, str):
        s = val.strip()
        if len(s) == 8 and s.isdigit():
            try:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            except ValueError:
                pass
    return None


def _parse_amount(val) -> Decimal | None:
    """Parse an amount from XLSX cell value.

    Handles:
    - Numeric values (int, float)
    - String amounts with $, commas ("1,000.00", "$100.00")
    - The trailing-CR credit marker card statements use, e.g. "100.00 CR"
      (CR = credit/payment, returned negative)
    """
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip().replace(",", "").replace("$", "")
        if not val or val == "-":
            return None
        # "100.00 CR" — a payment or refund on a card sheet.
        if val.upper().endswith(" CR"):
            val = val[:-3].strip()
            try:
                return -Decimal(val)
            except (InvalidOperation, ValueError):
                return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None


# ── Sheet parsers ───────────────────────────────────────────────────

def _is_header_row(row_values: tuple, header_keywords: set[str]) -> bool:
    """Check if a row looks like a header row."""
    if not row_values or row_values[0] is None:
        return False
    cells = [str(c or "").strip().lower() for c in row_values[:8]]
    matches = sum(1 for kw in header_keywords if any(kw in c for c in cells))
    return matches >= 3


def _is_all_none(row_values: tuple) -> bool:
    return all(v is None for v in row_values)


_CAD_USD_HEADER_KEYWORDS = {"transaction type", "date posted", "transaction amount", "description"}
_CC_HEADER_KEYWORDS = {"transaction date", "posting date", "transaction amount", "description"}


def _detect_currency_from_header(
    row_values: tuple,
    currencies: tuple[str, ...] = SECTION_CURRENCIES,
    default: str = DEFAULT_SECTION_CURRENCY,
) -> str:
    """The currency a section header names, or ``default`` when it names none."""
    for cell in row_values:
        if cell is None:
            continue
        lowered = str(cell).lower()
        for code in currencies:
            if code.lower() in lowered:
                return code
    return default


def _parse_cad_usd_sheet(ws, sheet_name: str) -> list[GroundTruthRow]:
    """Parse a sheet of currency sections stacked in one tab.

    The sheet has:
    - optional metadata row(s) at the top
    - a header row whose amount column names a currency ("Transaction Amount
      CAD", "Transaction Amount (CAD)")
    - that currency's data rows
    - blank rows, and optionally a SUM row, ending the section
    - the next section's header, and so on for as many as the tab holds
    """
    rows_out: list[GroundTruthRow] = []
    all_rows = list(ws.iter_rows(values_only=True))

    current_currency = None
    header_indices: dict[str, int] = {}
    data_started = False

    for row_idx, row_values in enumerate(all_rows, start=1):
        # Skip fully empty rows
        if _is_all_none(row_values):
            continue

        # Check for SUM row — signals end of current section
        cells_str = [str(c or "").strip().upper() for c in row_values[:8]]
        if any("SUM" in c for c in cells_str[:4]):
            data_started = False
            continue

        # Check if this is a header row
        if _is_header_row(row_values, _CAD_USD_HEADER_KEYWORDS):
            current_currency = _detect_currency_from_header(row_values)
            # Map column indices
            header_indices = {}
            for col_idx, cell in enumerate(row_values):
                if cell is None:
                    continue
                key = str(cell).strip().lower()
                if "transaction type" in key:
                    header_indices["txn_type"] = col_idx
                elif "date posted" in key:
                    header_indices["date"] = col_idx
                elif "transaction amount" in key:
                    header_indices["amount"] = col_idx
                elif "description" == key or key == "description":
                    header_indices["description"] = col_idx
                elif "category" in key:
                    header_indices["category"] = col_idx
                elif "invoice" in key or "cheque" in key:
                    header_indices["invoice"] = col_idx
                elif "note" in key:
                    header_indices["note"] = col_idx
            data_started = True
            continue

        if not data_started or current_currency is None:
            continue

        # Parse data row
        def _get(key: str, default="") -> str:
            idx = header_indices.get(key)
            if idx is None or idx >= len(row_values):
                return default
            v = row_values[idx]
            return str(v).strip() if v is not None else default

        date_val = row_values[header_indices["date"]] if "date" in header_indices and header_indices["date"] < len(row_values) else None
        amount_val = row_values[header_indices["amount"]] if "amount" in header_indices and header_indices["amount"] < len(row_values) else None

        parsed_date = _parse_date_value(date_val)
        parsed_amount = _parse_amount(amount_val)

        if parsed_date is None or parsed_amount is None:
            continue

        txn_type = _get("txn_type", "DEBIT" if parsed_amount < 0 else "CREDIT")
        description = _get("description")
        category = normalize_category(_get("category"))
        invoice = _get("invoice")
        note = _get("note")

        rows_out.append(GroundTruthRow(
            account=current_currency,
            transaction_type=txn_type.upper(),
            date_posted=parsed_date,
            amount=parsed_amount,
            description=description,
            category=category,
            invoice=invoice,
            note=note,
            sheet_name=sheet_name,
            row_number=row_idx,
        ))

    return rows_out


def _parse_credit_card_sheet(ws, sheet_name: str) -> list[GroundTruthRow]:
    """Parse a card sheet.

    Shape:
    - optional metadata and blank rows at the top
    - a header row carrying both a transaction date and a posting date, plus an
      amount and a description; the other columns are read when present
    - data rows until the sheet ends
    """
    rows_out: list[GroundTruthRow] = []
    all_rows = list(ws.iter_rows(values_only=True))

    header_indices: dict[str, int] = {}
    data_started = False

    for row_idx, row_values in enumerate(all_rows, start=1):
        if _is_all_none(row_values):
            continue

        # Find the header row
        if not data_started and _is_header_row(row_values, _CC_HEADER_KEYWORDS):
            for col_idx, cell in enumerate(row_values):
                if cell is None:
                    continue
                key = str(cell).strip().lower()
                if "posting date" in key:
                    header_indices["posting_date"] = col_idx
                elif "transaction date" in key:
                    header_indices["txn_date"] = col_idx
                elif "transaction amount" in key:
                    header_indices["amount"] = col_idx
                elif "description" == key or key == "description":
                    header_indices["description"] = col_idx
                elif "category" in key:
                    header_indices["category"] = col_idx
                elif "invoice" in key:
                    header_indices["invoice"] = col_idx
                elif "note" in key:
                    header_indices["note"] = col_idx
            data_started = True
            continue

        if not data_started:
            continue

        # Parse data row — need at least a date and amount
        def _get(key: str, default="") -> str:
            idx = header_indices.get(key)
            if idx is None or idx >= len(row_values):
                return default
            v = row_values[idx]
            return str(v).strip() if v is not None else default

        # Use posting date (when it hits the account), fall back to transaction date
        date_col = "posting_date" if "posting_date" in header_indices else "txn_date"
        date_val = row_values[header_indices[date_col]] if date_col in header_indices and header_indices[date_col] < len(row_values) else None
        amount_val = row_values[header_indices["amount"]] if "amount" in header_indices and header_indices["amount"] < len(row_values) else None

        parsed_date = _parse_date_value(date_val)
        parsed_amount = _parse_amount(amount_val)

        if parsed_date is None or parsed_amount is None:
            continue

        # Credit card: positive = charge (DEBIT from company's perspective),
        # negative = payment/refund (CREDIT)
        if parsed_amount < 0:
            txn_type = "CREDIT"
        else:
            txn_type = "DEBIT"

        description = _get("description")
        category = normalize_category(_get("category"))
        invoice = _get("invoice")
        note = _get("note")

        rows_out.append(GroundTruthRow(
            account="CreditCard",
            transaction_type=txn_type,
            date_posted=parsed_date,
            amount=parsed_amount,
            description=description,
            category=category,
            invoice=invoice,
            note=note,
            sheet_name=sheet_name,
            row_number=row_idx,
        ))

    return rows_out


# ── Public API ──────────────────────────────────────────────────────

def parse_xlsx(
    xlsx_path: Path,
    skip_sheets: Sequence[str] | None = None,
    sheet_kinds: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
) -> list[GroundTruthRow]:
    """Parse a ground-truth XLSX file into normalized GroundTruthRow records.

    Every sheet is classified by name: one whose name matches ``skip_sheets``
    (see ``DEFAULT_SKIP_SHEETS``) is reference material, one classified by
    ``sheet_kinds`` (see ``SHEET_KINDS``) is read with that kind's reader, and
    one that matches neither is left alone rather than guessed at.
    """
    if not xlsx_path.is_file():
        raise FileNotFoundError(f"XLSX not found: {xlsx_path}")

    skips = skip_sheets if skip_sheets is not None else DEFAULT_SKIP_SHEETS

    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    all_rows: list[GroundTruthRow] = []

    for sheet_name in wb.sheetnames:
        name_lower = sheet_name.lower()

        # Skip auxiliary sheets that hold no transaction rows
        if any(skip in name_lower for skip in skips):
            logger.debug("Skipping auxiliary sheet: %s", sheet_name)
            continue

        ws = wb[sheet_name]
        kind = sheet_kind(sheet_name, sheet_kinds)

        if kind == "card":
            rows = _parse_credit_card_sheet(ws, sheet_name)
            logger.info("Parsed %d card rows from '%s'", len(rows), sheet_name)
        elif kind == "currency_sections":
            rows = _parse_cad_usd_sheet(ws, sheet_name)
            logger.info("Parsed %d rows from '%s'", len(rows), sheet_name)
        else:
            logger.debug("Skipping sheet whose name names no kind: %s", sheet_name)
            continue

        all_rows.extend(rows)

    wb.close()
    return all_rows


def summarize_ground_truth(rows: list[GroundTruthRow]) -> None:
    """Print a summary of parsed ground truth data."""
    from collections import Counter

    accounts = Counter(r.account for r in rows)
    categories = Counter(r.category for r in rows)

    print(f"\nTotal rows: {len(rows)}")
    print("\nBy account:")
    for acct, count in sorted(accounts.items()):
        total = sum(r.amount for r in rows if r.account == acct)
        print(f"  {acct:<15} {count:>4} transactions, net: {total:>12}")

    print("\nBy category:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat:<35} {count:>4}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        # No argument: walk every month the manifest knows about. On a fresh
        # clone there are no books, so fall back to the synthetic fixture set.
        from scripts.qa.manifest import build_manifest, fixture_manifest
        manifest = build_manifest() or fixture_manifest()
        for key in sorted(manifest.keys()):
            md = manifest[key]
            if md.xlsx:
                print(f"\n{'='*60}")
                print(f"Parsing {md.label}: {md.xlsx}")
                print(f"{'='*60}")
                rows = parse_xlsx(md.xlsx)
                summarize_ground_truth(rows)
    else:
        xlsx_path = Path(sys.argv[1])
        rows = parse_xlsx(xlsx_path)
        summarize_ground_truth(rows)
