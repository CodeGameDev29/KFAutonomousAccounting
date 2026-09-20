"""Parse a per-account ground-truth workbook: one sheet per account per month.

The second of the two workbook shapes this harness reads. Where
``scripts/qa/xlsx_parser.py`` reads sections stacked in one tab, this one reads
a whole year from a single file with a tab per account per month, named so that
the tab says which account it holds and which month it covers::

    Jan2026_CAD          the CAD account's January rows
    Jan2026_USD          the USD account's January rows
    Jan2026_CreditCard   the card account's January rows
    Reference            any tab that carries no transactions, skipped

Which tab belongs to which account is :func:`account_tab_markers`, built from
the account keys in ``config/accounts.yaml`` so the roster is the one the rest
of the app uses; pass ``account_tabs=`` for a workbook that names its tabs some
other way. Tabs that hold no transactions are matched by
:data:`DEFAULT_SKIP_SHEETS`.

Usage:
    from scripts.qa.xlsx_parser_per_account import parse_per_account_xlsx
    rows = parse_per_account_xlsx(Path("data/books/2026-per-account.xlsx"))

Point ``QA_XLSX_PER_ACCOUNT`` at your own workbook, or pass the path as the one
argument. Nothing of the sort ships with this repository — ``data/`` is
gitignored.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl

from scripts.qa.manifest import account_of, configured_accounts
from scripts.qa.xlsx_parser import GroundTruthRow, normalize_category

logger = logging.getLogger(__name__)

# Tabs that are not per-account transaction data: an index, a running total,
# notes, a reference list. Substring matches against the lower-cased sheet
# name; edit this list, or pass `skip_sheets=`, to match your own workbook.
DEFAULT_SKIP_SHEETS: tuple[str, ...] = (
    "reference", "notes", "summary", "index", "contents", "total",
)


def account_tab_markers() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Account key -> the spellings a tab name may use for it.

    One entry per account in ``config/accounts.yaml``, longest key first so an
    account whose key contains another's is matched before it. Matching is a
    case-insensitive substring test against the tab name, which is why a key
    needs no separator convention: ``Jan2026_CreditCard`` and
    ``CreditCard Jan 2026`` both name the same account.
    """
    keys = sorted(configured_accounts(), key=len, reverse=True)
    return tuple(
        (key, tuple(dict.fromkeys(
            (key.lower(), key.lower().replace(" ", "_"), key.lower().replace(" ", ""))
        )))
        for key in keys
    )


def account_of_tab(
    sheet_name: str,
    account_tabs: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
) -> str | None:
    """Which account key a tab name names, or None when it names none.

    Configured accounts are tried first. A tab naming an account nobody has
    configured falls through to the shared filename spellings in
    ``scripts/qa/manifest.py``, so leaving an account commented out of
    ``config/accounts.yaml`` costs its rows a chosen position in a report and
    never the rows themselves.
    """
    lowered = sheet_name.lower()
    for account, markers in (account_tabs if account_tabs is not None
                             else account_tab_markers()):
        if any(m in lowered for m in markers):
            return account
    return None if account_tabs is not None else account_of(sheet_name)


def _parse_date_value(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            s = str(int(val))
            if len(s) == 8:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except (ValueError, OverflowError):
            pass
        return None
    if hasattr(val, "date"):
        return val.date() if callable(getattr(val, "date")) else val.date
    if isinstance(val, str):
        s = val.strip()
        if len(s) == 8 and s.isdigit():
            try:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
            except ValueError:
                pass
    return None


def _parse_amount(val) -> Decimal | None:
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip().replace(",", "").replace("$", "")
        if val.upper().endswith(" CR"):
            val = val[:-3].strip()
            try:
                return -Decimal(val)
            except (InvalidOperation, ValueError):
                return None
        if not val or val == "-":
            return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None


def _is_header_row(row_values, keywords: set[str]) -> bool:
    if not row_values or row_values[0] is None:
        return False
    cells = [str(c or "").strip().lower() for c in row_values[:8]]
    matches = sum(1 for kw in keywords if any(kw in c for c in cells))
    return matches >= 3


_CAD_USD_KEYWORDS = {"transaction type", "date posted", "transaction amount", "description"}
_CC_KEYWORDS = {"transaction date", "posting date", "transaction amount", "description"}


def _parse_cad_usd_sheet(ws, sheet_name: str, account: str) -> list[GroundTruthRow]:
    """Parse one bank account's tab: one date per row, signed amounts."""
    rows_out = []
    all_rows = list(ws.iter_rows(values_only=True))

    header_indices = {}
    data_started = False

    for row_idx, row_values in enumerate(all_rows, start=1):
        if all(v is None for v in (row_values or [])):
            continue

        # Check for SUM/NET row
        cells_str = [str(c or "").strip().upper() for c in row_values[:8]]
        if any("SUM" in c or "NET" in c for c in cells_str[:4]):
            break

        if not data_started and _is_header_row(row_values, _CAD_USD_KEYWORDS):
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
                elif key == "description":
                    header_indices["description"] = col_idx
                elif "category" in key:
                    header_indices["category"] = col_idx
                elif "invoice" in key or "cheque" in key:
                    header_indices["invoice"] = col_idx
                elif "note" in key:
                    header_indices["note"] = col_idx
            data_started = True
            continue

        if not data_started:
            continue

        def _get(key, default=""):
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
            account=account,
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


def _parse_cc_sheet(ws, sheet_name: str, account: str = "CreditCard") -> list[GroundTruthRow]:
    """Parse one card account's tab: two dates per row, a charge is positive."""
    rows_out = []
    all_rows = list(ws.iter_rows(values_only=True))

    header_indices = {}
    data_started = False

    for row_idx, row_values in enumerate(all_rows, start=1):
        if all(v is None for v in (row_values or [])):
            continue

        if not data_started and _is_header_row(row_values, _CC_KEYWORDS):
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
                elif key == "description":
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

        def _get(key, default=""):
            idx = header_indices.get(key)
            if idx is None or idx >= len(row_values):
                return default
            v = row_values[idx]
            return str(v).strip() if v is not None else default

        date_col = "posting_date" if "posting_date" in header_indices else "txn_date"
        date_val = row_values[header_indices[date_col]] if date_col in header_indices and header_indices[date_col] < len(row_values) else None
        amount_val = row_values[header_indices["amount"]] if "amount" in header_indices and header_indices["amount"] < len(row_values) else None

        parsed_date = _parse_date_value(date_val)
        parsed_amount = _parse_amount(amount_val)

        if parsed_date is None or parsed_amount is None:
            continue

        txn_type = "CREDIT" if parsed_amount < 0 else "DEBIT"
        description = _get("description")
        category = normalize_category(_get("category"))
        invoice = _get("invoice")
        note = _get("note")

        rows_out.append(GroundTruthRow(
            account=account,
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


def parse_per_account_xlsx(
    xlsx_path: Path,
    skip_sheets: Sequence[str] | None = None,
    account_tabs: tuple[tuple[str, tuple[str, ...]], ...] | None = None,
    card_accounts: Sequence[str] = ("CreditCard",),
) -> list[GroundTruthRow]:
    """Parse a per-account workbook into GroundTruthRow records.

    A sheet whose name contains one of ``skip_sheets`` is reference material
    (see ``DEFAULT_SKIP_SHEETS``). Every other sheet is read for the account its
    name points at (see :func:`account_tab_markers`), with the accounts named in
    ``card_accounts`` read as card statements: two dates per row, and a positive
    amount meaning a charge. A sheet naming no configured account is left alone
    and logged, rather than filed under a guess.
    """
    if not xlsx_path.is_file():
        raise FileNotFoundError(f"XLSX not found: {xlsx_path}")

    skips = skip_sheets if skip_sheets is not None else DEFAULT_SKIP_SHEETS
    cards = {a.lower() for a in card_accounts}

    wb = openpyxl.load_workbook(str(xlsx_path), data_only=True)
    all_rows = []

    for sheet_name in wb.sheetnames:
        name_lower = sheet_name.lower()

        if any(skip in name_lower for skip in skips):
            logger.debug("Skipping reference sheet: %s", sheet_name)
            continue

        account = account_of_tab(sheet_name, account_tabs)
        if account is None:
            logger.info(
                "Skipping '%s': its name names no account. Add the account to "
                "config/accounts.yaml, or pass account_tabs=, to read it.",
                sheet_name,
            )
            continue

        ws = wb[sheet_name]
        if account.lower() in cards:
            rows = _parse_cc_sheet(ws, sheet_name, account)
        else:
            rows = _parse_cad_usd_sheet(ws, sheet_name, account)
        logger.info("Parsed %d %s rows from '%s'", len(rows), account, sheet_name)

        all_rows.extend(rows)

    wb.close()
    return all_rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    import sys

    # No default workbook: the path is an argument or QA_XLSX_PER_ACCOUNT, and
    # a run with neither says so instead of failing on a path nobody chose.
    raw_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("QA_XLSX_PER_ACCOUNT")
    if not raw_path:
        print(
            "No workbook given. Pass one as an argument, or set "
            "QA_XLSX_PER_ACCOUNT:\n"
            "    python -m scripts.qa.xlsx_parser_per_account path/to/workbook.xlsx"
        )
        raise SystemExit(2)

    xlsx_path = Path(raw_path)
    rows = parse_per_account_xlsx(xlsx_path)

    from collections import Counter
    accounts = Counter(r.account for r in rows)
    months = Counter(r.date_posted.strftime("%Y-%m") for r in rows)

    print(f"\nTotal rows: {len(rows)}")
    print(f"\nBy account: {dict(accounts)}")
    print("\nBy month:")
    for m, cnt in sorted(months.items()):
        print(f"  {m}: {cnt}")

    # Placeholders a workbook uses for "no reference yet". Edit for your own.
    placeholders = {"", "-", "n/a", "na", "none", "tbd"}
    has_invoice = sum(
        1 for r in rows if r.invoice and r.invoice.strip().lower() not in placeholders
    )
    print(f"\nRows with invoice reference: {has_invoice}/{len(rows)}")
