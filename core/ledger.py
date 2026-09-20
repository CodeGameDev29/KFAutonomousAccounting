"""Ledger generation module — creates Excel workbooks and CSV exports.

CURRENCY RULE (non-negotiable)
------------------------------
An accounting package's bank-statement CSV import carries **one currency per
file**, taken from the bank account the file is imported into — never per row.
Xero and QuickBooks Online both work this way. So a single CSV holding CAD and
USD rows at their native amounts does not import as a multi-currency month:
every USD figure is booked as CAD, dollar for dollar, which overstates or
understates revenue and goes straight into a GST/HST or T2 filing.

Therefore :func:`export_xero_csv`, :func:`export_qbo_csv` and
:func:`export_transactions_csv` **refuse** a mixed-currency batch —
:class:`MixedCurrencyExportError` — rather than emit a file that cannot be
imported correctly. Callers split the month with :func:`group_by_currency`
and write one file per currency, with the ISO code in the filename. Every
row also carries an explicit ``Currency`` column so no amount can be read as
the wrong unit even if the file is opened by hand.

The engine does not record a transaction-level FX rate, so no export converts
an amount. Splitting is the only rule here that cannot produce a wrong number.
"""

from __future__ import annotations

import csv
from pathlib import Path

import openpyxl
from openpyxl.worksheet.hyperlink import Hyperlink

from config.ised_categories import gifi_for
from core.gst_tax_code import xero_tax_code
from core.spreadsheet_safety import SafeCsvWriter, neutralize_workbook_formulas
from core.transfer_exclusion import is_transfer_category
from models.ledger_entry import LedgerEntry
from models.transaction import AccountType, Transaction, account_str

MONTH_ABBRS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

LEDGER_HEADERS_STANDARD = [
    "Account Number",
    "Transaction Type",
    "Date Posted",
    "Transaction Amount",
    "Currency",
    "Description",
    "Category",
    "GIFI",
    "Invoice/Cheque",
    "Note",
]


class MixedCurrencyExportError(ValueError):
    """Raised when an export that carries one currency per file is handed several.

    Carries the currencies seen so the caller can name them in a user-facing
    message instead of silently writing a file that imports wrong.
    """

    def __init__(self, currencies):
        self.currencies = sorted(currencies)
        super().__init__(
            "This export writes one currency per file, because an accounting "
            "package takes the currency from the bank account a file is "
            "imported into. Got "
            + ", ".join(self.currencies)
            + " in one batch — split the batch with group_by_currency() and "
            "write one file per currency."
        )


def txn_currency(txn) -> str:
    """The transaction's ISO currency, defaulting to CAD only when unset."""
    return (getattr(txn, "currency", None) or "CAD").upper()


def group_by_currency(transactions) -> dict[str, list]:
    """Split transactions into ``{ISO code: rows}``, CAD first then alphabetical.

    The ordering is display order for filenames and manifests; it does not
    affect the rows inside a file (each file is still date-sorted).
    """
    grouped: dict[str, list] = {}
    for txn in transactions:
        grouped.setdefault(txn_currency(txn), []).append(txn)
    return {
        cur: grouped[cur]
        for cur in sorted(grouped, key=lambda c: (c != "CAD", c))
    }


def _single_currency(transactions) -> str:
    """Return the one currency in ``transactions``, or refuse the batch.

    An empty batch is CAD by convention so a header-only file is still
    unambiguous.
    """
    currencies = {txn_currency(t) for t in transactions}
    if len(currencies) > 1:
        raise MixedCurrencyExportError(currencies)
    return currencies.pop() if currencies else "CAD"


def month_label(year: int, month: int) -> str:
    """Return e.g. 'Jan2026' for month=1, year=2026."""
    return f"{MONTH_ABBRS[month - 1]}{year}"


def sheet_name(account, year: int, month: int, *, institution: str | None = None, account_type: str | None = None) -> str:
    """Return a sheet name for a ledger tab.

    Supports two calling signatures:

    Account form (an AccountType enum or a plain string):
        sheet_name(AccountType.CAD, 2026, 1)  → "Jan2026_CAD"

    Institution form (LLM parser path — institution + account_type strings):
        sheet_name(None, 2026, 1, institution="BMO", account_type="CHECKING")
            → "Jan2026_BMO_CHQ"
        sheet_name(None, 2026, 1, institution="Wise", account_type="FX_MULTI_CURRENCY")
            → "Jan2026_Wise_FX"

    Rules:
      - If `account` is an AccountType enum (or has a `.value` attribute), use
        the ``{month_label}_{account.value}`` format.
      - If `account` is a plain string, treat it as the suffix directly.
      - If `account` is None and `institution`/`account_type` kwargs are provided,
        build a composite suffix from them.
      - Sheet names are truncated to 31 chars (Excel limit).
    """
    prefix = month_label(year, month)

    if account is not None:
        # Account form: AccountType enum or plain string
        if hasattr(account, 'value'):
            suffix = account.value
        else:
            suffix = str(account)
        name = f"{prefix}_{suffix}"
    elif institution is not None or account_type is not None:
        # Institution form: build from institution + account_type
        inst = (institution or "Unknown").strip()
        # Shorten common account_type values to keep sheet names concise
        _TYPE_SHORT: dict[str, str] = {
            "CHECKING": "CHQ",
            "SAVINGS": "SAV",
            "CREDIT_CARD": "CC",
            "FX_MULTI_CURRENCY": "FX",
            "MERCHANT": "MRCH",
            "OTHER": "OTHER",
        }
        atype = account_type or "OTHER"
        short_type = _TYPE_SHORT.get(atype, atype)
        name = f"{prefix}_{inst}_{short_type}"
    else:
        name = f"{prefix}_Unknown"

    # Excel worksheet name limit is 31 characters
    return name[:31]


def write_ledger_entries(
    workbook: openpyxl.Workbook,
    entries: list[LedgerEntry],
    account: AccountType,
    year: int,
    month: int,
) -> None:
    """Write ledger entries to the appropriate sheet, appending to existing data."""
    sname = sheet_name(account, year, month)

    if sname in workbook.sheetnames:
        ws = workbook[sname]
    else:
        ws = workbook.create_sheet(title=sname)
        # Write headers as row 1
        for col_idx, header in enumerate(LEDGER_HEADERS_STANDARD, start=1):
            ws.cell(row=1, column=col_idx, value=header)

    for entry in entries:
        next_row = ws.max_row + 1
        date_int = int(entry.date_posted.strftime("%Y%m%d"))
        row_data = [
            account.value,
            entry.transaction_type,
            date_int,
            float(entry.amount),
            entry.currency,
            entry.description,
            entry.category,
            gifi_for(entry.category) or "",
            entry.document_link,
            entry.note,
        ]
        doc_col = LEDGER_HEADERS_STANDARD.index("Invoice/Cheque") + 1
        for col_idx, value in enumerate(row_data, start=1):
            cell = ws.cell(row=next_row, column=col_idx, value=value)
            # Invoice/Cheque — add hyperlink if document_link is set
            if col_idx == doc_col and entry.document_link is not None:
                cell.hyperlink = Hyperlink(ref=cell.coordinate, target=entry.document_link)


def generate_index_sheet(
    workbook: openpyxl.Workbook,
    months: list[str],
) -> None:
    """Create or get IndexSheet listing all monthly sheet names with internal hyperlinks."""
    if "IndexSheet" in workbook.sheetnames:
        ws = workbook["IndexSheet"]
    else:
        ws = workbook.create_sheet(title="IndexSheet")

    ws.cell(row=1, column=1, value="Sheet Index")

    for row_idx, sname in enumerate(months, start=2):
        cell = ws.cell(row=row_idx, column=1, value=sname)
        # Internal hyperlink to the sheet
        cell.hyperlink = Hyperlink(
            ref=cell.coordinate,
            location=f"'{sname}'!A1",
            display=sname,
        )


def generate_net_income_sheet(
    workbook: openpyxl.Workbook,
    monthly_totals: dict | None = None,
) -> None:
    """Create or get NET INCOME sheet with monthly summary data.

    Totals are per sheet **and per currency** — a Wise tab legitimately holds
    CAD and USD rows, and adding them would invent a number that is neither.
    ``monthly_totals`` maps a sheet key to either ``{"income": …,
    "expenses": …}`` (flat shape, taken as CAD) or ``{currency: {"income": …,
    "expenses": …}}``.
    """
    if "NET INCOME" in workbook.sheetnames:
        ws = workbook["NET INCOME"]
    else:
        ws = workbook.create_sheet(title="NET INCOME")

    ws.cell(row=1, column=1, value="Month")
    ws.cell(row=1, column=2, value="Currency")
    ws.cell(row=1, column=3, value="Income")
    ws.cell(row=1, column=4, value="Expenses")
    ws.cell(row=1, column=5, value="Net Income")

    row_idx = 2
    for month_key, totals in (monthly_totals or {}).items():
        # Flat shape: a bare {"income": …, "expenses": …} dict is CAD.
        by_currency = (
            {"CAD": totals}
            if "income" in totals or "expenses" in totals
            else totals
        )
        for currency in sorted(by_currency, key=lambda c: (c != "CAD", c)):
            bucket = by_currency[currency]
            income = bucket.get("income", 0)
            expenses = bucket.get("expenses", 0)
            ws.cell(row=row_idx, column=1, value=month_key)
            ws.cell(row=row_idx, column=2, value=currency)
            ws.cell(row=row_idx, column=3, value=float(income))
            ws.cell(row=row_idx, column=4, value=float(expenses))
            # income − expenses: both totals are stored as positive
            # magnitudes (see export_ledger), so adding them would
            # overstate net whenever DEBITs are stored positive.
            ws.cell(row=row_idx, column=5, value=float(income - expenses))
            row_idx += 1


def export_ledger(
    entries_by_month: dict[str, list[LedgerEntry]],
    output_path: Path,
    year: int = 2026,
) -> None:
    """Create a complete ledger workbook and save to output_path."""
    wb = openpyxl.Workbook()

    # Remove the default "Sheet" sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    monthly_totals: dict[str, dict] = {}
    sheet_names: list[str] = []

    for key, entries in entries_by_month.items():
        # Parse account type from key suffix (e.g., "Jan2026_CAD" -> "CAD")
        suffix = key.split("_", 1)[1] if "_" in key else key
        account = AccountType(suffix)

        # Parse month from key (e.g., "Jan2026" -> month number)
        month_str = key.split("_", 1)[0] if "_" in key else key
        abbr = month_str[:3]
        month_num = MONTH_ABBRS.index(abbr) + 1

        write_ledger_entries(wb, entries, account, year, month_num)
        sheet_names.append(key)

        # Compute monthly totals for NET INCOME, per currency. Internal
        # transfers (USD<->CAD moves, credit-card payments) are excluded so
        # money moving between own accounts isn't booked as income + expense.
        by_currency: dict[str, dict[str, float]] = {}
        for e in entries:
            if is_transfer_category(e.category):
                continue
            bucket = by_currency.setdefault(
                (e.currency or "CAD").upper(), {"income": 0.0, "expenses": 0.0}
            )
            side = "income" if e.transaction_type == "CREDIT" else "expenses"
            bucket[side] += abs(float(e.amount))
        monthly_totals[key] = by_currency or {"CAD": {"income": 0.0, "expenses": 0.0}}

    generate_index_sheet(wb, sheet_names)
    generate_net_income_sheet(wb, monthly_totals)

    neutralize_workbook_formulas(wb)
    wb.save(output_path)


def export_ledger_csv(
    entries_by_month: dict[str, list[LedgerEntry]],
    output_path: Path,
) -> None:
    """Export ledger entries as a single CSV file.

    All account sheets are combined into one CSV with an Account column.
    """
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = SafeCsvWriter(csv.writer(f))
        writer.writerow([
            "Account",
            "Transaction Type",
            "Date Posted",
            "Transaction Amount",
            "Currency",
            "Description",
            "Category",
            "GIFI",
            "Invoice/Cheque",
            "Note",
        ])

        for key, entries in entries_by_month.items():
            suffix = key.split("_", 1)[1] if "_" in key else key
            for entry in entries:
                writer.writerow([
                    suffix,
                    entry.transaction_type,
                    entry.date_posted.strftime("%Y-%m-%d"),
                    str(entry.amount),
                    entry.currency,
                    entry.description,
                    entry.category or "",
                    gifi_for(entry.category) or "",
                    entry.document_link or "",
                    entry.note or "",
                ])


def export_transactions_csv(
    transactions: list[Transaction],
    output_path: Path,
    matched_docs: dict[int, str] | None = None,
    categories: dict[int, str] | None = None,
    notes: dict[int, str] | None = None,
) -> None:
    """Export transactions as CSV in the standard ledger column layout.

    Format: Account Number | Transaction Type | Date Posted | Amount | Description | Category | Invoice/Cheque | Note

    This exports from the transactions table directly. All bank transactions
    are included regardless of reconciliation status. Invoice/Cheque shows
    the filename only (not the full path).

    Args:
        transactions: List of Transaction objects to export.
        output_path: Path to write the CSV file.
        matched_docs: Dict mapping transaction_id -> document stored_path.
        categories: Dict mapping transaction_id -> category string.
        notes: Dict mapping transaction_id -> note string.
    """
    if matched_docs is None:
        matched_docs = {}
    if categories is None:
        categories = {}
    if notes is None:
        notes = {}

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = SafeCsvWriter(csv.writer(f))
        writer.writerow([
            "Account Number",
            "Transaction Type",
            "Date Posted",
            "Transaction Amount",
            "Currency",
            "Description",
            "Category",
            "GIFI",
            "Invoice/Cheque",
            "Note",
        ])

        for txn in sorted(transactions, key=lambda t: t.date_posted):
            doc_path = matched_docs.get(txn.id, "")
            # Show filename only, not full path
            if doc_path:
                doc_filename = Path(doc_path).name
            elif hasattr(txn, 'status') and str(getattr(txn.status, 'value', txn.status)) == 'LINKED':
                doc_filename = "Linked (see Transfers sheet)"
            else:
                doc_filename = ""

            category = categories.get(txn.id, "")
            note = notes.get(txn.id, "")

            # Date as a YYYYMMDD integer, the column's ledger format
            date_int = int(txn.date_posted.strftime("%Y%m%d"))

            writer.writerow([
                account_str(txn.account),
                txn.transaction_type,
                date_int,
                str(txn.amount),
                txn_currency(txn),
                txn.description,
                category,
                gifi_for(category) or "",
                doc_filename,
                note,
            ])


QBO_HEADERS = [
    "Date", "Description", "Amount", "Currency", "Account", "Category", "GIFI",
]

XERO_HEADERS = [
    "Date", "Description", "Reference", "Amount", "Currency",
    "Account Code", "Tax Type", "Tax Basis", "GIFI",
]


def export_qbo_csv(
    transactions: list[Transaction],
    output_path: Path,
    matched_docs: dict[int, str] | None = None,
    categories: dict[int, str] | None = None,
) -> str:
    """Export **one currency** of transactions as a QuickBooks Online CSV.

    QBO CSV columns: Date, Description, Amount, Currency, Account, Category,
    GIFI. Date format MM/DD/YYYY; Amount negative for DEBIT, positive for
    CREDIT, always in the file's own currency (never converted).

    Raises :class:`MixedCurrencyExportError` if the batch holds more than one
    currency — QBO takes the currency from the bank account the file is
    imported into, so a mixed file books foreign amounts as CAD. Returns the
    ISO currency code the file was written in.

    A UTF-8 BOM is written for Excel compatibility.
    """
    if matched_docs is None:
        matched_docs = {}
    if categories is None:
        categories = {}

    file_currency = _single_currency(transactions)

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = SafeCsvWriter(csv.writer(f, quoting=csv.QUOTE_MINIMAL))
        writer.writerow(QBO_HEADERS)

        for txn in sorted(transactions, key=lambda t: t.date_posted):
            # MM/DD/YYYY date format
            date_str = txn.date_posted.strftime("%m/%d/%Y")

            # Negative for DEBIT, positive for CREDIT
            amount = float(txn.amount)
            if txn.transaction_type == "DEBIT":
                amount = -abs(amount)
            else:
                amount = abs(amount)

            account = account_str(txn.account)
            category = categories.get(txn.id) or txn.category or "Uncategorized"

            writer.writerow([
                date_str,
                txn.description,
                f"{amount:.2f}",
                txn_currency(txn),
                account,
                category,
                gifi_for(category) or "",
            ])

    return file_currency


def export_xero_csv(
    transactions: list[Transaction],
    output_path: Path,
    matched_docs: dict[int, str] | None = None,
    categories: dict[int, str] | None = None,
    doc_tax: dict[int, dict] | None = None,
    vendors: dict[int, str] | None = None,
) -> str:
    """Export **one currency** of transactions as a Xero compatible CSV.

    Columns: Date, Description, Reference, Amount, Currency, Account Code,
    Tax Type, Tax Basis, GIFI. Date format DD/MM/YYYY; Amount negative for
    DEBIT, positive for CREDIT, always in the file's own currency (never
    converted).

    Raises :class:`MixedCurrencyExportError` if the batch holds more than one
    currency — Xero takes the currency from the bank account the file is
    imported into. Returns the ISO currency code the file was written in.

    ``Tax Type`` / ``Tax Basis`` come from :func:`core.gst_tax_code.xero_tax_code`,
    which asserts a GST code only when the linked document actually shows
    GST/HST. ``doc_tax`` maps transaction id → ``{"has_document": bool,
    "gst": …, "hst": …}``; with no ``doc_tax`` the export can claim no input
    tax credit at all, which is the safe direction.

    A UTF-8 BOM is written for Excel compatibility.
    """
    if matched_docs is None:
        matched_docs = {}
    if categories is None:
        categories = {}
    if doc_tax is None:
        doc_tax = {}
    if vendors is None:
        vendors = {}

    file_currency = _single_currency(transactions)

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = SafeCsvWriter(csv.writer(f, quoting=csv.QUOTE_MINIMAL))
        writer.writerow(XERO_HEADERS)

        for txn in sorted(transactions, key=lambda t: t.date_posted):
            # DD/MM/YYYY date format
            date_str = txn.date_posted.strftime("%d/%m/%Y")

            # Negative for DEBIT, positive for CREDIT
            amount = float(txn.amount)
            if txn.transaction_type == "DEBIT":
                amount = -abs(amount)
            else:
                amount = abs(amount)

            # Reference is the matched doc filename (no path)
            doc_path = matched_docs.get(txn.id, "")
            reference = Path(doc_path).name if doc_path else ""

            account_code = account_str(txn.account)
            category = categories.get(txn.id) or txn.category or None
            vendor = vendors.get(txn.id)
            if isinstance(vendor, list):
                vendor = vendor[0] if vendor else None
            tax_type, tax_basis = xero_tax_code(
                category,
                doc_tax=doc_tax.get(txn.id),
                description=txn.description,
                vendor=vendor,
            )

            writer.writerow([
                date_str,
                txn.description,
                reference,
                f"{amount:.2f}",
                txn_currency(txn),
                account_code,
                tax_type,
                tax_basis,
                gifi_for(category) or "",
            ])

    return file_currency
