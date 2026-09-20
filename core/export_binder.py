"""Record binder (ZIP) generation for one month of transactions.

Produces an unencrypted ZIP containing:
  - XLSX ledger workbook: Summary tab, one tab per account, Transfers tab
  - proof_of_transaction/ folder with all matched source documents
  - README.txt explaining the contents and record-keeping guidance

The ZIP is written unencrypted so any reviewer can open it without a
password or a key exchange.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

from config.data_files import account_display, account_order
from core.html_binder import (
    STATUS_LABELS,
    _build_category_breakdown,
    _sum_currency_totals,
    binder_proof_counts,
    generate_html_report,
)
from core.transfer_exclusion import is_transfer_category
from models.transaction import TransactionStatus, account_str


def _is_internal_transfer(txn, transfer_txn_ids: set[int] | None) -> bool:
    """Internal transfer by link (precomputed ids) or transfer-like category.

    These rows stay listed in the ledger so an auditor can see them, but are
    excluded from Income/Expenses/Net totals to avoid double-counting money
    moved between the user's own accounts.
    """
    if transfer_txn_ids and txn.id is not None and txn.id in transfer_txn_ids:
        return True
    return is_transfer_category(getattr(txn, "category", None))

logger = logging.getLogger(__name__)

# ─── File store (required — no in-repo fallback) ─────────────────────────
from core.file_storage import download_file as storage_download_file
from core.file_storage import storage_root

# ─── Account display names (string keys — txn.account is a free-form string) ─
# Both tables come from config/accounts.yaml; edit that file to rename an
# account or change the order it appears in.
ACCOUNT_DISPLAY = account_display()

# Preferred display order for known account types. Any account key not listed
# here (custom LLM-parsed institutions, etc.) is appended alphabetically after
# these so PayPal, Amazon, BMO, etc. are never silently dropped.
ACCOUNT_ORDER_KEYS = account_order()


def _ordered_account_keys(keys) -> list:
    """Order account keys: known ones first, rest alphabetically."""
    seen: set = set()
    ordered: list = []
    for k in ACCOUNT_ORDER_KEYS:
        if k in keys and k not in seen:
            ordered.append(k)
            seen.add(k)
    for k in sorted(keys):
        if k not in seen:
            ordered.append(k)
            seen.add(k)
    return ordered

# ─── XLSX styling constants ──────────────────────────────────────────────────
# Palette mirrors the binder's index.html so both artifacts read as one report:
# brand-blue headers, green money-in, red money-out, muted transfer rows, amber
# uncategorized flags, and the same status-badge colors.
BRAND_BLUE = "0F3460"
BRAND_DARK = "1A1A2E"
INCOME_GREEN = "1E7E46"
EXPENSE_RED = "C22F3E"
MUTED_GRAY = "6C757D"
ROW_EVEN_BG = "F6F8FA"
BORDER_GRAY = "DEE2E6"
CHIP_BG = "EEF2F7"
TOTAL_BG = "F0F3F7"
SUBTOTAL_BG = "FBFCFD"
UNCAT_AMBER = "856404"

HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color=BRAND_BLUE, end_color=BRAND_BLUE, fill_type="solid")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)
HEADER_BORDER = Border(
    bottom=Side(style="medium", color=BRAND_BLUE),
)
BODY_FONT = Font(name="Calibri", size=10)
CREDIT_FONT = Font(name="Calibri", size=10, color=INCOME_GREEN)
DEBIT_FONT = Font(name="Calibri", size=10, color=EXPENSE_RED)
# Transfer rows stay listed for auditability but are visually de-emphasized —
# they are excluded from every total.
TRANSFER_FONT = Font(name="Calibri", size=10, color=MUTED_GRAY, italic=True)
UNCAT_FONT = Font(name="Calibri", size=10, color=UNCAT_AMBER, italic=True)
TOTAL_FONT = Font(name="Calibri", bold=True, size=11)
TOTAL_CREDIT_FONT = Font(name="Calibri", bold=True, size=11, color=INCOME_GREEN)
TOTAL_DEBIT_FONT = Font(name="Calibri", bold=True, size=11, color=EXPENSE_RED)
TOTAL_FILL = PatternFill(start_color="E8ECF0", end_color="E8ECF0", fill_type="solid")
EVEN_ROW_FILL = PatternFill(start_color=ROW_EVEN_BG, end_color=ROW_EVEN_BG, fill_type="solid")
CHIP_FILL = PatternFill(start_color=CHIP_BG, end_color=CHIP_BG, fill_type="solid")
TOTAL_BG_FILL = PatternFill(start_color=TOTAL_BG, end_color=TOTAL_BG, fill_type="solid")
SUBTOTAL_FILL = PatternFill(start_color=SUBTOTAL_BG, end_color=SUBTOTAL_BG, fill_type="solid")
ROW_BORDER = Border(bottom=Side(style="thin", color=BORDER_GRAY))
SECTION_LABEL_FONT = Font(name="Calibri", bold=True, size=9, color=MUTED_GRAY)
SECTION_HDR_FONT = Font(name="Calibri", bold=True, size=9, color=BRAND_BLUE)
GIFI_FONT = Font(name="Consolas", size=9, color=MUTED_GRAY)
TILE_LABEL_FONT = Font(name="Calibri", size=9, color=MUTED_GRAY)
TILE_CREDIT_FONT = Font(name="Calibri", bold=True, size=14, color=INCOME_GREEN)
TILE_DEBIT_FONT = Font(name="Calibri", bold=True, size=14, color=EXPENSE_RED)
STAT_VALUE_FONT = Font(name="Calibri", bold=True, size=12, color=BRAND_DARK)
AMOUNT_FORMAT = '#,##0.00'
DATE_FORMAT = "YYYY-MM-DD"

# Status badge colors (background, foreground) — identical to the index.html
# .badge-* classes so a status reads the same in Excel and in the browser.
STATUS_BADGE = {
    "MATCHED": ("D4EDDA", "155724"),
    "UNMATCHED": ("F8D7DA", "721C24"),
    # Green, not grey: "no receipt needed" is a resolved state and its money is
    # counted in every total on this sheet. Grey belongs to EXCLUDED alone.
    "IGNORED": ("E8F5E9", "1B5E20"),
    "LINKED": ("D1ECF1", "0C5460"),
    "PENDING_REVIEW": ("FFF3CD", "856404"),
    "EXCLUDED": ("E2E3E5", "383D41"),
}
STATUS_FILLS = {k: PatternFill(start_color=bg, end_color=bg, fill_type="solid")
                for k, (bg, _fg) in STATUS_BADGE.items()}
STATUS_FONTS = {k: Font(name="Calibri", size=10, color=fg)
                for k, (_bg, fg) in STATUS_BADGE.items()}

# Sheet tab colors — Summary blue, category P&L green, accounts slate,
# Transfers amber — so the workbook's structure is visible from the tab bar.
TAB_SUMMARY = BRAND_BLUE
TAB_CATEGORY = INCOME_GREEN
TAB_ACCOUNT = "8496B0"
TAB_TRANSFERS = "BF8F00"

CATEGORY_SHEET_TITLE = "Where Money Went"


def _cur_fmt(currency: str | None) -> str:
    """Excel number format with an explicit currency marker (C$ / US$ / ISO).

    Mirrors index.html's _format_currency so no amount is ever mislabeled
    as dollars when a sheet mixes currencies.
    """
    cur = currency or "CAD"
    if cur == "CAD":
        return '"C$"#,##0.00'
    if cur == "USD":
        return '"US$"#,##0.00'
    return f'"{cur} "#,##0.00'

# Column order mirrors the in-app Transactions table (Date, Description,
# Category, Amount, Status, ...) so the exported ledger reads the same way
# as the web UI. Amounts are signed by transaction type (CREDIT +, DEBIT −)
# regardless of how the source parser stored the raw sign.
LEDGER_COLUMNS = [
    ("Date", 12),
    ("Description", 42),
    ("Category", 26),
    ("Amount", 14),
    ("Currency", 10),
    ("Type", 10),
    ("Status", 15),
    ("Vendor", 22),
    ("Notes", 30),
    ("Proof of Transaction", 45),
]

# Explicit +/− sign, matching the index.html amount display; color comes from
# the cell font (green credit / red debit), not the number format.
AMOUNT_FORMAT_SIGNED = '+#,##0.00;-#,##0.00'
TITLE_FONT = Font(name="Calibri", bold=True, size=13, color=BRAND_BLUE)
SUBTITLE_FONT = Font(name="Calibri", color=MUTED_GRAY, size=9)
# Hyperlink cells (proof files, cross-sheet navigation) — Excel's link blue.
HLINK_FONT = Font(name="Calibri", size=10, color="0563C1", underline="single")


def _safe_filename(text: str) -> str:
    """Sanitize a string for use as a filename component."""
    safe = re.sub(r'[<>:"/\\|?*]', '_', text)
    safe = re.sub(r'\s+', '_', safe)
    safe = safe.strip('._')
    return safe[:80] if safe else "unknown"


def _build_xlsx(
    company_name: str,
    year: int,
    month: int,
    label: str,
    transactions: list,
    doc_paths: dict[int, str | list[str]],
    vendors: dict[int, str | list[str]],
    receipt_name_map: dict[int, str | list[str]] | None = None,
    transfer_txn_ids: set[int] | None = None,
    transaction_links: list[dict] | None = None,
    proof_rel_paths: dict[str, str] | None = None,
) -> bytes:
    """Build the XLSX ledger workbook and return as bytes.

    One workbook with tabs: Summary, one sheet per account (titled with the
    account display name + period, columns mirroring the in-app Transactions
    table), and — when inter-account links exist — a Transfers sheet
    documenting both sides of every internal transfer.
    Internal transfers are listed in account sheets but excluded from totals.

    ``proof_rel_paths`` maps each archived proof filename to its path inside
    the ZIP (e.g. ``proof_of_transaction/20000115-Orchid_Telecom-104.55-CAD.pdf``).
    When provided, every proof cell becomes a RELATIVE hyperlink — Excel
    resolves it against the workbook's folder, so clicking the cell opens
    the bundled file directly once the ZIP is extracted.
    """
    import calendar as _calendar
    month_name = _calendar.month_name[month]

    wb = openpyxl.Workbook()

    # Remove default sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    # Group transactions by account (a free-form string).
    txn_by_account: dict[str, list] = {}
    for txn in transactions:
        key = account_str(txn.account) or "Unknown"
        txn_by_account.setdefault(key, []).append(txn)

    sheets_created = 0
    # account key → actual sheet title, for cross-sheet navigation links
    sheet_title_by_account: dict[str, str] = {}

    for acct_key in _ordered_account_keys(txn_by_account.keys()):
        acct_txns = txn_by_account.get(acct_key)
        if not acct_txns:
            continue

        # XLSX sheet titles: max 31 chars, no special chars
        raw_title = f"{label}_{acct_key}"
        sheet_title = re.sub(r'[\[\]:*?/\\]', '_', raw_title)[:31]
        # Avoid duplicate titles (e.g. two accounts slugged identically after truncation)
        base_title = sheet_title
        dup_idx = 2
        while sheet_title in wb.sheetnames:
            suffix = f"_{dup_idx}"
            sheet_title = base_title[:31 - len(suffix)] + suffix
            dup_idx += 1
        ws = wb.create_sheet(title=sheet_title)
        sheets_created += 1
        sheet_title_by_account[acct_key] = sheet_title

        # ── Title block (rows 1-2) so the tab is self-describing ──────────
        acct_display = ACCOUNT_DISPLAY.get(acct_key, acct_key)
        ws.cell(row=1, column=1, value=f"{acct_display} — {month_name} {year}")
        ws.cell(row=1, column=1).font = TITLE_FONT
        ws.cell(row=2, column=1, value=f"{company_name} · Transaction ledger · amounts signed by type (+ money in / − money out)")
        ws.cell(row=2, column=1).font = SUBTITLE_FONT

        # ── Header row (row 3) ────────────────────────────────────────────
        header_row = 3
        for col_idx, (header_name, width) in enumerate(LEDGER_COLUMNS, start=1):
            cell = ws.cell(row=header_row, column=col_idx, value=header_name)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGNMENT
            cell.border = HEADER_BORDER
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        # ── Data rows ─────────────────────────────────────────────────────
        sorted_txns = sorted(acct_txns, key=lambda t: (t.date_posted, t.id or 0))
        # Totals per currency — a sheet (e.g. Wise) can hold several
        # currencies, and amounts in different currencies must never be
        # numerically summed into one figure.
        totals_by_currency: dict[str, dict[str, Decimal]] = {}

        first_data_row = header_row + 1
        for row_idx, txn in enumerate(sorted_txns, start=first_data_row):
            # Proof of transaction file label + click target.
            # proof_target: relative file path inside the extracted ZIP;
            # proof_location: internal workbook location ('Transfers'!A1).
            proof_file = ""
            proof_target: str | None = None
            proof_location: str | None = None
            if receipt_name_map and txn.id is not None and txn.id in receipt_name_map:
                names = receipt_name_map[txn.id]
                if isinstance(names, list):
                    proof_file = names[0] + (f" +{len(names)-1} more" if len(names) > 1 else "")
                    first_name = names[0] if names else ""
                else:
                    proof_file = names
                    first_name = names
                if proof_rel_paths and first_name:
                    proof_target = proof_rel_paths.get(first_name)
            elif txn.id is not None and txn.id in doc_paths:
                paths = doc_paths[txn.id]
                if isinstance(paths, list):
                    proof_file = Path(paths[0]).name + (f" +{len(paths)-1} more" if len(paths) > 1 else "")
                else:
                    proof_file = Path(paths).name
            elif txn.status == TransactionStatus.LINKED:
                proof_file = "Linked (see Transfers sheet)"
                if transaction_links:
                    proof_location = "'Transfers'!A1"
            elif txn.status == TransactionStatus.IGNORED:
                proof_file = "N/A"
            else:
                proof_file = "Missing"

            vendor_val = vendors.get(txn.id, "") if txn.id is not None else ""
            vendor = vendor_val[0] if isinstance(vendor_val, list) and vendor_val else (vendor_val if isinstance(vendor_val, str) else "")
            category = txn.category or ""
            note = txn.note or ""
            status_value = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
            status_label = STATUS_LABELS.get(status_value, status_value.title())

            # Sign comes from the transaction TYPE, not the stored sign —
            # raw signs vary by ingest path (CSV import stores debits negative,
            # the LLM parser stores magnitudes) and would render
            # inconsistently across sheets.
            signed_amount = float(abs(txn.amount)) if txn.transaction_type == "CREDIT" else -float(abs(txn.amount))

            row_data = [
                txn.date_posted,
                txn.description,
                category,
                signed_amount,
                txn.currency,
                txn.transaction_type,
                status_label,
                vendor,
                note,
                proof_file,
            ]

            is_transfer_row = _is_internal_transfer(txn, transfer_txn_ids)
            row_font = TRANSFER_FONT if is_transfer_row else BODY_FONT
            is_even = (row_idx - first_data_row) % 2 == 1

            for col_idx, value in enumerate(row_data, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.font = row_font
                cell.border = ROW_BORDER
                if is_even:
                    cell.fill = EVEN_ROW_FILL

                # Date formatting
                if col_idx == 1 and isinstance(value, date):
                    cell.number_format = DATE_FORMAT
                    cell.alignment = Alignment(horizontal="center")
                # NULL category → amber "Uncategorized" flag (mirrors index.html)
                elif col_idx == 3 and not category:
                    cell.value = "Uncategorized"
                    if not is_transfer_row:
                        cell.font = UNCAT_FONT
                # Amount: explicit +/− sign, green credit / red debit
                elif col_idx == 4:
                    cell.number_format = AMOUNT_FORMAT_SIGNED
                    cell.alignment = Alignment(horizontal="right")
                    if not is_transfer_row:
                        cell.font = CREDIT_FONT if txn.transaction_type == "CREDIT" else DEBIT_FONT
                # Center currency, type
                elif col_idx in (5, 6):
                    cell.alignment = Alignment(horizontal="center")
                # Status: colored badge, same palette as the HTML report
                elif col_idx == 7:
                    cell.alignment = Alignment(horizontal="center")
                    if status_value in STATUS_BADGE:
                        cell.fill = STATUS_FILLS[status_value]
                        cell.font = STATUS_FONTS[status_value]

            # Make the proof cell clickable: a RELATIVE link to the archived
            # file (resolved against the workbook's folder after the ZIP is
            # extracted), or an internal jump to the Transfers sheet for
            # linked internal transfers.
            proof_cell = ws.cell(row=row_idx, column=len(LEDGER_COLUMNS))
            if proof_target:
                proof_cell.hyperlink = Hyperlink(
                    ref=proof_cell.coordinate, target=proof_target,
                )
                proof_cell.font = HLINK_FONT
            elif proof_location:
                proof_cell.hyperlink = Hyperlink(
                    ref=proof_cell.coordinate, location=proof_location,
                )
                proof_cell.font = HLINK_FONT

            # Accumulate totals — internal transfers are listed above but
            # never counted, otherwise a USD->CAD move books as income on
            # one sheet and expense on another (double-counted net).
            if not is_transfer_row:
                cur = txn.currency or "CAD"
                bucket = totals_by_currency.setdefault(
                    cur, {"income": Decimal("0"), "expenses": Decimal("0"), "count": 0}
                )
                bucket["count"] += 1
                if txn.transaction_type == "CREDIT":
                    bucket["income"] += abs(txn.amount)
                else:
                    bucket["expenses"] += abs(txn.amount)

        # ── Totals block — a labelled mini-table, one row per currency ────
        transfer_count = sum(
            1 for t in sorted_txns if _is_internal_transfer(t, transfer_txn_ids)
        )
        r = first_data_row + len(sorted_txns) + 1
        ws.cell(row=r, column=1, value="TOTALS")
        ws.cell(row=r, column=1).font = TOTAL_FONT
        if transfer_count:
            ws.cell(row=r, column=2,
                    value=f"{transfer_count} internal transfer row(s) excluded — see Transfers sheet")
            ws.cell(row=r, column=2).font = SUBTITLE_FONT
        r += 1

        totals_headers = ["Currency", "Money in", "Money out", "Net", "Transactions"]
        for col_idx, h in enumerate(totals_headers, start=1):
            cell = ws.cell(row=r, column=col_idx, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGNMENT
        r += 1

        currency_keys = sorted(totals_by_currency.keys(), key=lambda c: (c != "CAD", c != "USD", c))
        if not currency_keys:
            ws.cell(row=r, column=1, value="All rows are internal transfers — excluded from totals")
            ws.cell(row=r, column=1).font = TOTAL_FONT
            for col_idx in range(1, 6):
                ws.cell(row=r, column=col_idx).fill = TOTAL_FILL
        for cur in currency_keys:
            bucket = totals_by_currency[cur]
            income = bucket["income"]
            expenses = bucket["expenses"]
            net = income - expenses
            values = [cur, float(income), float(expenses), float(net), bucket["count"]]
            for col_idx, value in enumerate(values, start=1):
                cell = ws.cell(row=r, column=col_idx, value=value)
                cell.fill = TOTAL_FILL
                if col_idx == 2:
                    cell.font = TOTAL_CREDIT_FONT
                elif col_idx == 3:
                    cell.font = TOTAL_DEBIT_FONT
                elif col_idx == 4:
                    cell.font = TOTAL_CREDIT_FONT if net >= 0 else TOTAL_DEBIT_FONT
                else:
                    cell.font = TOTAL_FONT
                if col_idx in (2, 3, 4):
                    cell.number_format = _cur_fmt(cur)
                    cell.alignment = Alignment(horizontal="right")
                elif col_idx in (1, 5):
                    cell.alignment = Alignment(horizontal="center")
            r += 1

        # Freeze title + header rows
        ws.freeze_panes = f"A{first_data_row}"
        # Clean report look (no gridlines), colored tab, and column filters
        # so the sheet is sortable/filterable out of the box.
        ws.sheet_view.showGridLines = False
        ws.sheet_properties.tabColor = TAB_ACCOUNT
        if sorted_txns:
            ws.auto_filter.ref = (
                f"A{header_row}:{get_column_letter(len(LEDGER_COLUMNS))}"
                f"{first_data_row + len(sorted_txns) - 1}"
            )
        # Back-link to the Summary hub (mirrors index.html's sidebar nav).
        nav_cell = ws.cell(row=1, column=len(LEDGER_COLUMNS), value="← Summary")
        nav_cell.hyperlink = Hyperlink(ref=nav_cell.coordinate, location="'Summary'!A1")
        nav_cell.font = HLINK_FONT
        nav_cell.alignment = Alignment(horizontal="right")

    # If no transactions produced any sheets, create a placeholder
    if sheets_created == 0:
        ws = wb.create_sheet(title=f"{label}_Empty")
        ws.cell(row=1, column=1, value="No transactions for this period.")

    # ── Summary sheet — the workbook's hub, mirroring index.html's
    # Executive Summary + Reconciliation Status + per-account nav ─────────
    def _is_transfer_txn(t) -> bool:
        return _is_internal_transfer(t, transfer_txn_ids)

    ws_summary = wb.create_sheet(title="Summary", index=0)
    ws_summary.sheet_view.showGridLines = False
    ws_summary.sheet_properties.tabColor = TAB_SUMMARY

    ws_summary.cell(row=1, column=1, value=f"{company_name} — {month_name} {year}")
    ws_summary.cell(row=1, column=1).font = Font(name="Calibri", bold=True, size=16, color=BRAND_BLUE)
    ws_summary.cell(row=2, column=1, value=(
        f"Monthly audit ledger · Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"{len(transactions)} transactions · one tab per account · "
        "internal transfers documented on the Transfers tab"
    ))
    ws_summary.cell(row=2, column=1).font = SUBTITLE_FONT

    # Quick navigation, like the index.html sidebar.
    nav_cell = ws_summary.cell(row=3, column=1, value="→ Where the money went")
    nav_cell.hyperlink = Hyperlink(
        ref=nav_cell.coordinate, location=f"'{CATEGORY_SHEET_TITLE}'!A1",
    )
    nav_cell.font = HLINK_FONT
    if transaction_links:
        nav_cell = ws_summary.cell(row=3, column=2, value="→ Internal transfers")
        nav_cell.hyperlink = Hyperlink(ref=nav_cell.coordinate, location="'Transfers'!A1")
        nav_cell.font = HLINK_FONT

    # ── Executive summary tiles: per-currency Money in / out / Net over
    # non-transfer transactions — the same numbers index.html shows. ──────
    row = 5
    ws_summary.cell(row=row, column=1, value="EXECUTIVE SUMMARY").font = SECTION_LABEL_FONT
    row += 1
    exec_totals = _sum_currency_totals(transactions, _is_transfer_txn)
    if not exec_totals:
        ws_summary.cell(
            row=row, column=1,
            value="Every transaction this month is an internal transfer between "
                  "your own accounts — see the Transfers tab.",
        ).font = SUBTITLE_FONT
        row += 1
    for ct in exec_totals:
        if len(exec_totals) > 1:
            ws_summary.cell(
                row=row, column=1,
                value=f"{ct['currency']} · {ct['txn_count']} transactions",
            ).font = SECTION_LABEL_FONT
            row += 1
        for col_idx, lab in enumerate(("Money in", "Money out", "Net (in − out)"), start=1):
            ws_summary.cell(row=row, column=col_idx, value=lab).font = TILE_LABEL_FONT
        row += 1
        net_positive = ct["net"] >= 0
        tiles = [
            (float(ct["income"]), TILE_CREDIT_FONT, INCOME_GREEN),
            (float(ct["expenses"]), TILE_DEBIT_FONT, EXPENSE_RED),
            (float(ct["net"]),
             TILE_CREDIT_FONT if net_positive else TILE_DEBIT_FONT,
             INCOME_GREEN if net_positive else EXPENSE_RED),
        ]
        for col_idx, (val, tile_font, edge_color) in enumerate(tiles, start=1):
            cell = ws_summary.cell(row=row, column=col_idx, value=val)
            cell.font = tile_font
            cell.number_format = _cur_fmt(ct["currency"])
            # Colored left edge, like the index.html tile border.
            cell.border = Border(left=Side(style="thick", color=edge_color))
        row += 2

    # ── Reconciliation status (matched / linked / documented rate / …) ───
    status_counts = {"MATCHED": 0, "LINKED": 0, "PENDING_REVIEW": 0, "UNMATCHED": 0, "IGNORED": 0}
    for t in transactions:
        s = t.status.value if hasattr(t.status, "value") else str(t.status)
        status_counts[s] = status_counts.get(s, 0) + 1
    reconcilable = len(transactions) - status_counts["IGNORED"]
    documented = status_counts["MATCHED"] + status_counts["LINKED"]
    documented_rate = round(documented / reconcilable * 100, 1) if reconcilable else 0.0

    ws_summary.cell(row=row, column=1, value="RECONCILIATION STATUS").font = SECTION_LABEL_FONT
    row += 1
    recon_rows = [
        ("Matched to a receipt / invoice", status_counts["MATCHED"], None),
        ("Linked (transfer counterpart found)", status_counts["LINKED"], None),
        (f"Documented ({documented} of {reconcilable} reconcilable)", documented_rate, '0.0"%"'),
        ("Unmatched (no supporting document yet)", status_counts["UNMATCHED"], None),
        ("Pending review", status_counts["PENDING_REVIEW"], None),
        ("No receipt needed (self-evident — amounts still counted)",
         status_counts["IGNORED"], None),
    ]
    for label_txt, val, fmt in recon_rows:
        lab_cell = ws_summary.cell(row=row, column=1, value=label_txt)
        lab_cell.font = BODY_FONT
        lab_cell.border = ROW_BORDER
        val_cell = ws_summary.cell(row=row, column=2, value=val)
        val_cell.font = STAT_VALUE_FONT
        val_cell.alignment = Alignment(horizontal="center")
        val_cell.border = ROW_BORDER
        if fmt:
            val_cell.number_format = fmt
        row += 1
    ws_summary.cell(
        row=row, column=1,
        value='"Documented" counts transactions matched to a proof document or '
              "linked to their transfer counterpart, out of all non-ignored transactions.",
    ).font = SUBTITLE_FONT
    row += 2

    # ── Accounts table (click an account name to jump to its tab) ────────
    ws_summary.cell(row=row, column=1, value="ACCOUNTS").font = SECTION_LABEL_FONT
    row += 1
    summary_headers = ["Account", "Currency", "Transactions", "Money in", "Money out", "Net"]
    for col_idx, h in enumerate(summary_headers, start=1):
        cell = ws_summary.cell(row=row, column=col_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
    row += 1

    def _currency_order(cur: str) -> tuple:
        return (cur != "CAD", cur != "USD", cur)

    # currency → {"income": Decimal, "expenses": Decimal, "count": int}
    grand_by_currency: dict[str, dict] = {}

    for acct_key in _ordered_account_keys(txn_by_account.keys()):
        acct_txns = txn_by_account.get(acct_key)
        if not acct_txns:
            continue

        # One summary row per currency within the account — a Wise account
        # can hold USD/EUR/CAD rows and those must never be summed together.
        by_cur: dict[str, dict] = {}
        for t in acct_txns:
            cur = t.currency or "CAD"
            bucket = by_cur.setdefault(
                cur, {"income": Decimal("0"), "expenses": Decimal("0"), "count": 0}
            )
            bucket["count"] += 1
            if _is_internal_transfer(t, transfer_txn_ids):
                continue
            if t.transaction_type == "CREDIT":
                bucket["income"] += abs(t.amount)
            else:
                bucket["expenses"] += abs(t.amount)

        for cur in sorted(by_cur.keys(), key=_currency_order):
            bucket = by_cur[cur]
            income = bucket["income"]
            expenses = bucket["expenses"]
            net = income - expenses
            acct_cell = ws_summary.cell(row=row, column=1, value=ACCOUNT_DISPLAY.get(acct_key, acct_key))
            # Click an account name to jump to its ledger tab.
            acct_sheet = sheet_title_by_account.get(acct_key)
            if acct_sheet:
                acct_cell.hyperlink = Hyperlink(
                    ref=acct_cell.coordinate, location=f"'{acct_sheet}'!A1",
                )
                acct_cell.font = HLINK_FONT
            ws_summary.cell(row=row, column=2, value=cur)
            ws_summary.cell(row=row, column=2).alignment = Alignment(horizontal="center")
            ws_summary.cell(row=row, column=3, value=bucket["count"])
            ws_summary.cell(row=row, column=3).alignment = Alignment(horizontal="center")
            ws_summary.cell(row=row, column=4, value=float(income))
            ws_summary.cell(row=row, column=4).font = CREDIT_FONT
            ws_summary.cell(row=row, column=5, value=float(expenses))
            ws_summary.cell(row=row, column=5).font = DEBIT_FONT
            ws_summary.cell(row=row, column=6, value=float(net))
            ws_summary.cell(row=row, column=6).font = CREDIT_FONT if net >= 0 else DEBIT_FONT
            for col_idx in range(1, 7):
                ws_summary.cell(row=row, column=col_idx).border = ROW_BORDER
                if col_idx >= 4:
                    ws_summary.cell(row=row, column=col_idx).number_format = _cur_fmt(cur)
                    ws_summary.cell(row=row, column=col_idx).alignment = Alignment(horizontal="right")

            g = grand_by_currency.setdefault(
                cur, {"income": Decimal("0"), "expenses": Decimal("0"), "count": 0}
            )
            g["income"] += income
            g["expenses"] += expenses
            g["count"] += bucket["count"]
            row += 1

    # Grand totals — one row per currency; never a cross-currency sum.
    for cur in sorted(grand_by_currency.keys(), key=_currency_order):
        g = grand_by_currency[cur]
        g_net = g["income"] - g["expenses"]
        for col_idx in range(1, 7):
            ws_summary.cell(row=row, column=col_idx).fill = TOTAL_FILL
            ws_summary.cell(row=row, column=col_idx).font = TOTAL_FONT
            ws_summary.cell(row=row, column=col_idx).border = Border(
                top=Side(style="medium", color=BRAND_BLUE),
            )
        ws_summary.cell(row=row, column=1, value=f"TOTAL ({cur})")
        ws_summary.cell(row=row, column=2, value=cur)
        ws_summary.cell(row=row, column=2).alignment = Alignment(horizontal="center")
        ws_summary.cell(row=row, column=3, value=g["count"])
        ws_summary.cell(row=row, column=3).alignment = Alignment(horizontal="center")
        ws_summary.cell(row=row, column=4, value=float(g["income"]))
        ws_summary.cell(row=row, column=4).font = TOTAL_CREDIT_FONT
        ws_summary.cell(row=row, column=5, value=float(g["expenses"]))
        ws_summary.cell(row=row, column=5).font = TOTAL_DEBIT_FONT
        ws_summary.cell(row=row, column=6, value=float(g_net))
        ws_summary.cell(row=row, column=6).font = (
            TOTAL_CREDIT_FONT if g_net >= 0 else TOTAL_DEBIT_FONT
        )
        for col_idx in range(4, 7):
            ws_summary.cell(row=row, column=col_idx).number_format = _cur_fmt(cur)
            ws_summary.cell(row=row, column=col_idx).alignment = Alignment(horizontal="right")
        row += 1

    # Exclusion footnote so an auditor understands why row amounts and
    # totals can differ.
    note_row = row + 1
    ws_summary.cell(
        row=note_row,
        column=1,
        value="Note: internal transfers between own accounts (incl. USD/CAD "
              "conversions and credit-card payments) are listed in account "
              "sheets but excluded from Money in / Money out / Net totals — "
              "each one is documented on the Transfers tab. Totals are per "
              "currency — amounts in different currencies are never added "
              "together.",
    )
    ws_summary.cell(row=note_row, column=1).font = Font(
        name="Calibri", italic=True, color="6C757D", size=9
    )

    # Column widths for summary
    for col_idx, width in enumerate([34, 16, 16, 16, 16, 16], start=1):
        ws_summary.column_dimensions[get_column_letter(col_idx)].width = width

    # ── "Where the money went" tab — category P&L grouped by ISED/GIFI
    # section, right after Summary (mirrors index.html's category tables) ─
    _add_category_pl_sheet(
        wb, company_name, month_name, year, transactions, transfer_txn_ids,
    )

    # ── Transfers tab (last) — both sides of every internal transfer ─────
    _add_transfers_sheet(
        wb, company_name, month_name, year, transactions, transaction_links,
        sheet_title_by_account=sheet_title_by_account,
    )

    # Serialize
    xlsx_buf = io.BytesIO()
    wb.save(xlsx_buf)
    xlsx_bytes = xlsx_buf.getvalue()
    xlsx_buf.close()
    return xlsx_bytes


def _missing_proof_rows(
    transactions: list,
    verified_receipt_map: dict,
    doc_paths: dict,
    proof_failures: dict[str, str],
) -> list[tuple[object, str]]:
    """Every MATCHED row with no proof in this ZIP, paired with the reason why.

    Same predicate ``binder_proof_counts`` uses for ``matched_no_proof_file``,
    so the README's list can never disagree with the README's count above it.

    Three distinct causes, which a bare count folds into one misleading
    sentence ("proof file not in this ZIP"):

      * nothing APPROVED is linked to the row — its match record is gone, or is
        still sitting in Review — so the MATCHED status is stale drift and there
        never was a file to archive;
      * the linked document carries no stored path;
      * the file the path names is not on the disk any more.

    Only the last two are "not in this ZIP". Telling the user which one it is,
    per transaction, is the difference between a number and something to act on.
    """
    rows: list[tuple[object, str]] = []
    for txn in transactions:
        status = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
        if status != "MATCHED":
            continue
        if txn.id is not None and txn.id in (verified_receipt_map or {}):
            continue
        raw = (doc_paths or {}).get(txn.id)
        paths = [p for p in (raw if isinstance(raw, list) else [raw]) if p is not None]
        if not paths:
            rows.append((txn, _NO_MATCH_RECORD))
            continue
        reasons: list[str] = []
        for path in paths:
            reason = proof_failures.get(path)
            if reason is None:
                reason = _NO_PATH_ON_RECORD if not str(path).strip() else _FILE_GONE
            if reason not in reasons:
                reasons.append(reason)
        rows.append((txn, "; ".join(reasons)))
    return rows


def _format_missing_proofs(rows: list[tuple[object, str]]) -> str:
    """The README's "Matched, proof file missing" block. Empty when nothing is."""
    if not rows:
        return ""
    count = len(rows)
    lines = [
        "",
        "Matched, proof file missing",
        "---------------------------",
        f"{count} transaction{'' if count == 1 else 's'} "
        f"{'is' if count == 1 else 'are'} marked matched but no proof file for "
        f"{'it' if count == 1 else 'them'} reached this ZIP.",
        "Each one says why, so it can be fixed — an auditor cannot substantiate",
        "these rows as they stand.",
        "",
    ]
    for txn, reason in rows:
        posted = (
            txn.date_posted.strftime("%Y-%m-%d")
            if getattr(txn, "date_posted", None) else "unknown date"
        )
        description = (getattr(txn, "description", "") or "").strip() or "(no description)"
        if len(description) > 56:
            description = description[:53] + "..."
        try:
            amount = f"{txn.amount:,.2f}"
        except (TypeError, ValueError):
            amount = str(getattr(txn, "amount", ""))
        currency = getattr(txn, "currency", None) or "CAD"
        lines.append(f"  {posted}  {amount} {currency}  {description}")
        lines.append(f"      {reason}")
    lines.append("")
    return "\n".join(lines)


def _build_readme(
    company_name: str,
    label: str,
    year: int,
    month: int,
    txn_count: int,
    receipt_count: int,
    statement_count: int = 0,
    counts: dict | None = None,
    missing_proofs: list[tuple[object, str]] | None = None,
) -> str:
    """Build README.txt content for the record binder.

    ``counts`` comes from :func:`core.html_binder.binder_proof_counts` — the
    same call that fills index.html's Reconciliation Status section, so the two
    documents inside one ZIP cannot disagree. When it is supplied,
    ``Proofs of Transaction`` is the number of files actually written into
    ``proof_of_transaction/`` and the block below it says what happened to
    every other row, including the rows index.html badges "Linked to transfer"
    (matched to a counterpart transaction, not to a receipt).
    """
    import calendar
    month_name = calendar.month_name[month]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    counts = counts or {}
    proof_files = counts.get("proof_files", receipt_count)
    missing_block = _format_missing_proofs(missing_proofs or [])
    missing_pointer = (
        '\n  Listed one by one, with the reason for each, under'
        ' "Matched, proof file missing" below.' if missing_block else ""
    )
    breakdown = ""
    if counts:
        breakdown = f"""
How every transaction is supported
----------------------------------
Substantiated by a proof in this ZIP:      {counts.get('with_proof', 0)}
  (files under proof_of_transaction/:      {proof_files})
Linked to a transfer counterpart:          {counts.get('linked_to_transfer', 0)}
  NOT a receipt. index.html badges these rows "Linked to transfer": the row
  was matched to the other side of an internal transfer, which shows the money
  moved between your own accounts. It is not proof of a purchase or a sale, and
  these rows have no file in proof_of_transaction/.
Matched, proof file missing:               {counts.get('matched_no_proof_file', 0)}{missing_pointer}
Pending review:                            {counts.get('pending_review', 0)}
No supporting document yet:                {counts.get('no_document', 0)}
No receipt needed (fees, interest,
  internal transfers — amounts still
  counted in every total):                 {counts.get('no_receipt_needed', 0)}
Rejected at import (counted nowhere):      {counts.get('excluded', 0)}

Transactions needing a proof:              {counts.get('reconcilable', 0)}
Substantiated:                             {counts.get('substantiated_rate', 0.0)}%
{missing_block}"""

    return f"""{company_name} — Record Binder
{'=' * 50}

Report Period:           {month_name} {year} ({label})
Generated:               {now}
Transactions:            {txn_count}
Proofs of Transaction:   {proof_files}
Source Statements:       {statement_count}
{breakdown}
Contents
--------
index.html
    Interactive HTML report. Open in any web browser (offline).
    Shows transactions grouped by account with clickable proof-of-transaction
    links. Each account section also links to the original uploaded
    statement document(s) under statements/, so you can verify the table
    against the source. IMPORTANT: Extract the entire ZIP before opening
    index.html.

{_safe_filename(company_name)}_{label}_Ledger.xlsx
    Transaction ledger workbook (styled to match index.html):
      - Summary tab: executive summary (per-currency Money in / Money out /
        Net), reconciliation status with documented rate, and a per-account
        per-currency table — account names link to their tabs
      - Where Money Went tab: category P&L grouped by ISED section with
        CRA GIFI line codes, per-currency subtotals and totals
      - One tab per account: date, description, category, amount (signed:
        + money in / - money out, green/red), currency, type, color-coded
        status, vendor, notes, and proof-of-transaction reference. Columns
        are filterable; internal transfer rows are greyed out. Proof cells
        are clickable hyperlinks that open the archived file — keep the
        extracted folder structure intact so the relative links resolve.
      - Transfers tab: both sides of every internal transfer between your
        own accounts (these rows are excluded from all totals)

proof_of_transaction/{'' if proof_files else '   (NOT PRESENT — no proof was archived for this month)'}
    Matched source documents (proofs of transaction) linked to transactions.
    Files are named {{YYYYMMDD}}-{{VendorName}}-{{Amount}}-{{Currency}}.pdf
    for easy cross-referencing with the ledger.

statements/{'' if statement_count else '   (NOT PRESENT — no statement file was archived)'}
    Original bank, Wise, credit-card, and PayPal statement files that were
    uploaded to produce the transaction ledger. index.html links to each of
    these from the relevant account section so you can verify the generated
    table against the actual statement document line by line.

Record-keeping guidance
-----------------------
Canadian record-keeping guidance (CRA Information Circular IC05-1R1) expects
business records to be kept for at least six years from the end of the last
tax year they relate to. This binder is designed around that guidance; it is
not a certification, and nobody has certified this software against it. The
records in question include:
  - Source documents (proofs of transaction)
  - Accounting records and ledgers
  - Bank statements

This ZIP is unencrypted so any reviewer can open it without a password.

Where this data lives
---------------------
Autonomous Accounting is self-hosted: your transactions and your source
documents sit on a single self-hosted machine, on its local disk. There
is no cloud object store, no provider-managed encryption at rest, no SOC 2
audit and no point-in-time recovery behind them. Keep your own copy of this
ZIP — it is a complete, offline, unencrypted record of the month.

Generated by Autonomous Accounting
{os.environ.get('PUBLIC_SITE_URL', '')}
"""


# Bound concurrent file-store downloads: on Windows, many simultaneous
# requests through the HTTP/2 client fail with WinError 10035 ("non-blocking
# socket operation could not be completed immediately"). The cap keeps the
# binder responsive without exhausting the connection pool.
_STORAGE_FETCH_SEMAPHORE: asyncio.Semaphore | None = None


def _get_storage_semaphore() -> asyncio.Semaphore:
    global _STORAGE_FETCH_SEMAPHORE
    if _STORAGE_FETCH_SEMAPHORE is None:
        _STORAGE_FETCH_SEMAPHORE = asyncio.Semaphore(4)
    return _STORAGE_FETCH_SEMAPHORE


def _storage_path_candidates(path: str | None) -> list[str]:
    """Every spelling a stored path may have, most likely first.

    ``documents.stored_path`` can hold any of several spellings, and the
    resolver has to understand all of them or it reports a file "not in this
    ZIP" while the bytes sit on the disk under another spelling:

      * canonical — ``{user_id}/{YYYYMM}/{hash16}_{filename}``
      * the same, behind an ``uploads/`` prefix the file store does not use
      * Windows-written rows — backslash separators, and occasionally a full
        absolute path that already includes STORAGE_ROOT

    An empty or whitespace path yields NO candidates, deliberately. Falling
    through to ``resolve_path("")`` would resolve to the storage root
    *directory*, so a document row with a null path fails with a read error on
    a folder, indistinguishable from a deleted file.
    """
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return []

    candidates: list[str] = []

    def _add(value: str) -> None:
        value = value.strip().strip("/")
        if value and value not in candidates:
            candidates.append(value)

    _add(raw)
    # An absolute path under the storage root is the same file addressed the
    # long way round.
    try:
        root = storage_root().as_posix().rstrip("/")
    except Exception:  # pragma: no cover - storage misconfigured
        root = ""
    if root and raw.lower().startswith(root.lower() + "/"):
        _add(raw[len(root) + 1:])
    if raw.startswith("uploads/"):
        _add(raw[len("uploads/"):])
    return candidates


# What the README says about a proof that did not make it into the ZIP. Phrased
# for the person who has to act on it, not for a log.
_NO_PATH_ON_RECORD = (
    "the document linked to it has no stored file, so there was nothing to "
    "archive. Re-upload the receipt."
)
_FILE_GONE = (
    "its proof file is no longer in the file store. Re-upload the receipt."
)
_NO_MATCH_RECORD = (
    "no approved proof is linked to it, so its matched status is stale. Approve "
    "or reject the pair in Review, or attach a receipt to it."
)


async def _fetch_receipt_bytes(path: str, user_id: str) -> tuple[bytes | None, str | None]:
    """Download a proof or statement file from the file store.

    Returns ``(bytes, None)`` on success and ``(None, reason)`` on failure,
    where ``reason`` is a sentence the binder README can show the user. A bare
    count of proofs missing from the ZIP is a dead end; the reason is what
    makes it actionable.
    """
    candidates = _storage_path_candidates(path)
    if not candidates:
        logger.warning("proof document has no stored path (user %s)", user_id)
        return None, _NO_PATH_ON_RECORD

    sem = _get_storage_semaphore()
    loop = asyncio.get_event_loop()
    last_err: Exception | None = None
    async with sem:
        for candidate in candidates:
            try:
                data = await loop.run_in_executor(
                    None, storage_download_file, candidate
                )
                if data:
                    return data, None
            except Exception as e:
                last_err = e
                continue

    logger.warning(
        "Failed to download receipt from storage: %s (tried %s) — %s",
        path,
        ", ".join(candidates),
        last_err,
    )
    return None, _FILE_GONE


def _add_category_pl_sheet(
    wb: openpyxl.Workbook,
    company_name: str,
    month_name: str,
    year: int,
    transactions: list,
    transfer_txn_ids: set[int] | None = None,
) -> None:
    """Add the "Where Money Went" tab — per-currency category P&L grouped by
    ISED section with GIFI line codes, mirroring index.html's category tables.

    Reuses html_binder's _build_category_breakdown so the XLSX and the HTML
    report always agree on every number.
    """
    def _is_tr(t) -> bool:
        return _is_internal_transfer(t, transfer_txn_ids)

    breakdowns = _build_category_breakdown(transactions, _is_tr)

    ws = wb.create_sheet(title=CATEGORY_SHEET_TITLE, index=1)
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = TAB_CATEGORY

    ws.cell(row=1, column=1, value=f"Where the Money Went — {month_name} {year}")
    ws.cell(row=1, column=1).font = TITLE_FONT
    ws.cell(row=2, column=1, value=(
        f"{company_name} · every transaction grouped by category, following the "
        "ISED / CRA GIFI income-statement structure · internal transfers excluded"
    ))
    ws.cell(row=2, column=1).font = SUBTITLE_FONT

    nav_cell = ws.cell(row=1, column=6, value="← Summary")
    nav_cell.hyperlink = Hyperlink(ref=nav_cell.coordinate, location="'Summary'!A1")
    nav_cell.font = HLINK_FONT
    nav_cell.alignment = Alignment(horizontal="right")

    for col_idx, width in enumerate([36, 10, 13, 16, 16, 16], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    r = 4
    if not breakdowns:
        ws.cell(
            row=r, column=1,
            value="Every transaction this month is an internal transfer between "
                  "your own accounts, so there is no income or spending to categorize.",
        ).font = SUBTITLE_FONT
        return

    multi_currency = len(breakdowns) > 1
    headers = ["Category", "GIFI", "Transactions", "Money in", "Money out", "Net"]

    for bd in breakdowns:
        cur = bd["currency"]
        caption = f"Category breakdown — {cur}" if multi_currency else "Category breakdown"
        ws.cell(row=r, column=1, value=caption).font = Font(
            name="Calibri", bold=True, size=12, color=BRAND_DARK,
        )
        r += 1

        for col_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=r, column=col_idx, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = HEADER_ALIGNMENT
            cell.border = HEADER_BORDER
        r += 1

        for sec in bd["sections"]:
            # Section chip row (REVENUE / COST OF SALES / OPERATING / …)
            for col_idx in range(1, 7):
                cell = ws.cell(row=r, column=col_idx)
                cell.fill = CHIP_FILL
                cell.border = ROW_BORDER
            ws.cell(row=r, column=1, value=sec["label"].upper()).font = SECTION_HDR_FONT
            r += 1

            for cat_row in sec["rows"]:
                is_uncat = cat_row["category"] == "Uncategorized"
                name_cell = ws.cell(row=r, column=1, value=cat_row["category"])
                name_cell.font = UNCAT_FONT if is_uncat else BODY_FONT
                gifi_cell = ws.cell(row=r, column=2, value=cat_row["gifi"] or "")
                gifi_cell.font = GIFI_FONT
                gifi_cell.alignment = Alignment(horizontal="center")
                count_cell = ws.cell(row=r, column=3, value=cat_row["txn_count"])
                count_cell.font = BODY_FONT
                count_cell.alignment = Alignment(horizontal="center")
                money_cells = [
                    (4, cat_row["income"], CREDIT_FONT),
                    (5, cat_row["expenses"], DEBIT_FONT),
                    (6, cat_row["net"],
                     CREDIT_FONT if cat_row["net"] >= 0 else DEBIT_FONT),
                ]
                for col_idx, val, font in money_cells:
                    # Blank instead of 0.00 for empty in/out, like the HTML "—"
                    cell = ws.cell(
                        row=r, column=col_idx,
                        value=float(val) if (val or col_idx == 6) else None,
                    )
                    cell.font = font
                    cell.number_format = _cur_fmt(cur)
                    cell.alignment = Alignment(horizontal="right")
                for col_idx in range(1, 7):
                    ws.cell(row=r, column=col_idx).border = ROW_BORDER
                r += 1

            if len(sec["rows"]) > 1:
                for col_idx in range(1, 7):
                    cell = ws.cell(row=r, column=col_idx)
                    cell.fill = SUBTOTAL_FILL
                    cell.border = ROW_BORDER
                ws.cell(row=r, column=1, value=f"{sec['label']} subtotal").font = Font(
                    name="Calibri", bold=True, size=10,
                )
                sub_cells = [
                    (4, sec["income"], INCOME_GREEN),
                    (5, sec["expenses"], EXPENSE_RED),
                    (6, sec["net"],
                     INCOME_GREEN if sec["net"] >= 0 else EXPENSE_RED),
                ]
                for col_idx, val, color in sub_cells:
                    # Blank instead of 0.00 for empty in/out, like the HTML "—"
                    cell = ws.cell(
                        row=r, column=col_idx,
                        value=float(val) if (val or col_idx == 6) else None,
                    )
                    cell.font = Font(name="Calibri", bold=True, size=10, color=color)
                    cell.number_format = _cur_fmt(cur)
                    cell.alignment = Alignment(horizontal="right")
                r += 1

        # Per-currency total row
        for col_idx in range(1, 7):
            cell = ws.cell(row=r, column=col_idx)
            cell.fill = TOTAL_BG_FILL
            cell.border = Border(top=Side(style="medium", color=BRAND_BLUE))
        ws.cell(row=r, column=1, value=f"Total ({cur})").font = TOTAL_FONT
        total_cells = [
            (4, bd["income"], TOTAL_CREDIT_FONT),
            (5, bd["expenses"], TOTAL_DEBIT_FONT),
            (6, bd["net"],
             TOTAL_CREDIT_FONT if bd["net"] >= 0 else TOTAL_DEBIT_FONT),
        ]
        for col_idx, val, font in total_cells:
            cell = ws.cell(row=r, column=col_idx, value=float(val))
            cell.font = font
            cell.number_format = _cur_fmt(cur)
            cell.alignment = Alignment(horizontal="right")
        r += 3  # spacer before the next currency's table


def _add_transfers_sheet(
    wb: openpyxl.Workbook,
    company_name: str,
    month_name: str,
    year: int,
    transactions: list,
    transaction_links: list[dict] | None = None,
    sheet_title_by_account: dict[str, str] | None = None,
) -> None:
    """Add a Transfers tab to the ledger workbook.

    Lists every linked transaction pair with both sides documented, so an
    auditor's question about an unexplained deposit is answered in advance.
    Transfer-type links are excluded from income/expense totals; refund/
    reversal links (REFUND/OTHER, e.g. a same-account NSF return) remain
    included in totals — the subtitle must not claim otherwise.
    No-op if there are no linked transactions.
    """
    if not transaction_links:
        return

    ws = wb.create_sheet(title="Transfers")
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = TAB_TRANSFERS

    # Title block — same shape as the account sheets
    ws.cell(row=1, column=1, value=f"Linked Transactions — {month_name} {year}")
    ws.cell(row=1, column=1).font = TITLE_FONT
    ws.cell(row=2, column=1,
            value=f"{company_name} · both sides of every linked pair shown. "
                  "Transfer-type links (credit card payment, internal transfer, "
                  "FX conversion, Wise transfer) are excluded from income/expense "
                  "totals; refund/reversal links remain included")
    ws.cell(row=2, column=1).font = SUBTITLE_FONT

    headers = [
        ("From Account", 20), ("From Date", 12), ("From Amount", 14),
        ("From Currency", 12), ("From Description", 35),
        ("To Account", 20), ("To Date", 12), ("To Amount", 14),
        ("To Currency", 12), ("To Description", 35),
        ("Link Type", 18), ("Confidence", 12), ("Status", 16), ("Explanation", 40),
    ]
    header_row = 3
    for col_idx, (name, width) in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = HEADER_BORDER
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Build txn lookup
    txn_lookup = {txn.id: txn for txn in transactions if txn.id is not None}

    def _acct_display(txn) -> str:
        key = account_str(txn.account) if txn else ""
        return ACCOUNT_DISPLAY.get(key, key)

    def _signed(txn) -> float:
        # Same sign convention as the account sheets: type decides the sign.
        if txn is None:
            return 0.0
        mag = float(abs(txn.amount))
        return mag if txn.transaction_type == "CREDIT" else -mag

    row_idx = header_row + 1
    for link in transaction_links:
        src_txn = txn_lookup.get(link.get("source_transaction_id"))
        tgt_txn = txn_lookup.get(link.get("target_transaction_id"))

        values = [
            _acct_display(src_txn),
            src_txn.date_posted if src_txn else "",
            _signed(src_txn) if src_txn else 0,
            src_txn.currency if src_txn else "",
            src_txn.description if src_txn else "",
            _acct_display(tgt_txn),
            tgt_txn.date_posted if tgt_txn else "",
            _signed(tgt_txn) if tgt_txn else 0,
            tgt_txn.currency if tgt_txn else "",
            tgt_txn.description if tgt_txn else "",
            (link.get("link_type") or "").replace("_", " ").title(),
            link.get("confidence_score", ""),
            (link.get("status") or "").replace("_", " ").title(),
            link.get("explanation", "") or "",
        ]
        is_even = (row_idx - header_row) % 2 == 0
        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.font = BODY_FONT
            cell.border = ROW_BORDER
            if is_even:
                cell.fill = EVEN_ROW_FILL
            if col_idx in (2, 7) and isinstance(value, date):
                cell.number_format = DATE_FORMAT
                cell.alignment = Alignment(horizontal="center")
            elif col_idx in (3, 8):
                cell.number_format = AMOUNT_FORMAT_SIGNED
                cell.alignment = Alignment(horizontal="right")
                if isinstance(value, (int, float)):
                    cell.font = CREDIT_FONT if value >= 0 else DEBIT_FONT
            elif col_idx in (4, 9, 11, 12, 13):
                cell.alignment = Alignment(horizontal="center")

        # From/To account cells jump back to that account's ledger tab.
        for col_idx, side_txn in ((1, src_txn), (6, tgt_txn)):
            if side_txn is None or not sheet_title_by_account:
                continue
            acct_sheet = sheet_title_by_account.get(account_str(side_txn.account) or "")
            if acct_sheet:
                acct_cell = ws.cell(row=row_idx, column=col_idx)
                acct_cell.hyperlink = Hyperlink(
                    ref=acct_cell.coordinate, location=f"'{acct_sheet}'!A1",
                )
                acct_cell.font = HLINK_FONT
        row_idx += 1

    ws.freeze_panes = f"A{header_row + 1}"
    if row_idx > header_row + 1:
        ws.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(len(headers))}{row_idx - 1}"
        )
    nav_cell = ws.cell(row=1, column=len(headers), value="← Summary")
    nav_cell.hyperlink = Hyperlink(ref=nav_cell.coordinate, location="'Summary'!A1")
    nav_cell.font = HLINK_FONT
    nav_cell.alignment = Alignment(horizontal="right")


async def generate_audit_binder(
    company_name: str,
    year: int,
    month: int,
    label: str,
    transactions: list,
    doc_paths: dict[int, str | list[str]],
    vendors: dict[int, str | list[str]],
    user_id: str,
    transaction_links: list[dict] | None = None,
    statement_paths: dict[str, str] | None = None,
    transfer_txn_ids: set[int] | None = None,
) -> bytes:
    """Generate the record binder ZIP for one month.

    The ZIP is unencrypted so any reviewer can open it, and contains:
      - XLSX ledger with per-account sheets and summary
      - proof_of_transaction/ folder with all matched source documents
      - statements/ folder with the original uploaded bank/wise statement
        files, so an auditor (or the user) can verify each account's
        transaction table directly against the source document.
      - README.txt describing the contents, with record-keeping guidance

    Args:
        company_name: Business name for headers and filenames.
        year: Report year (e.g. 2026).
        month: Report month (1-12).
        label: Month label (e.g. "Mar2026").
        transactions: List of Transaction model instances.
        doc_paths: Mapping of transaction_id -> stored receipt path in the file store.
        vendors: Mapping of transaction_id -> vendor display name.
        user_id: User ID for storage access control.
        statement_paths: Optional map of `source_file` (the original filename
            that was uploaded — already present on every Transaction) to its
            stored path in the file store. When provided, the binder fetches
            each statement, bundles it under `statements/`, and the HTML
            report renders a per-account download link.

    Returns:
        ZIP file content as bytes (unencrypted).
    """
    safe_company = _safe_filename(company_name)

    # ── Build standardised proof-of-transaction filename map ─────────────
    # Format: {YYYYMMDD}-{VendorName}-{Amount}-{Currency}.pdf
    # Maps every txn_id → filename(s) used inside the ZIP so that XLSX, HTML,
    # and ZIP all use identical names.
    # For multi-proof transactions, receipt_name_map[txn_id] is a list of filenames.
    receipt_name_map: dict[int, str | list[str]] = {}   # txn_id → ZIP filename(s)
    path_to_filename: dict[str, str] = {}   # original path → ZIP filename
    used_filenames: set[str] = set()
    multi_proof_txns: set[int] = set()     # txn_ids with >1 proof

    # Build a fast lookup from txn_id → transaction
    txn_lookup: dict[int, object] = {txn.id: txn for txn in transactions if txn.id is not None}

    # Normalize doc_paths: ensure each value is a list
    def _as_list(v: str | list[str]) -> list[str]:
        return v if isinstance(v, list) else [v]

    # Filter doc_paths to only include transactions in this batch.
    for txn_id, paths_raw in doc_paths.items():
        if txn_id not in txn_lookup:
            continue

        paths = _as_list(paths_raw)
        if len(paths) > 1:
            multi_proof_txns.add(txn_id)

        txn = txn_lookup.get(txn_id)
        date_str = txn.date_posted.strftime("%Y%m%d") if txn and txn.date_posted else "00000000"
        vendor_raw = vendors.get(txn_id, "Unknown")
        vendor_str = _safe_filename(vendor_raw[0] if isinstance(vendor_raw, list) and vendor_raw else (vendor_raw if isinstance(vendor_raw, str) else "Unknown"))
        amount_str = f"{abs(txn.amount):.2f}" if txn else "0.00"
        currency = (txn.currency if txn and txn.currency else "CAD")

        txn_filenames: list[str] = []
        for path in paths:
            if path in path_to_filename:
                txn_filenames.append(path_to_filename[path])
                continue

            # Preserve original extension (almost always .pdf)
            ext = Path(path).suffix or ".pdf"
            base_name = f"{date_str}-{vendor_str}-{amount_str}-{currency}{ext}"

            # De-duplicate: append -2, -3, … on collision
            filename = base_name
            counter = 2
            while filename in used_filenames:
                stem = base_name.rsplit('.', 1)[0]
                filename = f"{stem}-{counter}{ext}"
                counter += 1

            used_filenames.add(filename)
            path_to_filename[path] = filename
            txn_filenames.append(filename)

        receipt_name_map[txn_id] = txn_filenames if len(txn_filenames) > 1 else (txn_filenames[0] if txn_filenames else "")

    # ── Fetch proofs of transaction concurrently ──────────────────────────
    receipt_tasks: list[tuple[str, str, asyncio.Task]] = []

    for path, zip_filename in path_to_filename.items():
        task = asyncio.ensure_future(_fetch_receipt_bytes(path, user_id))
        receipt_tasks.append((path, zip_filename, task))

    # Await all downloads
    receipt_files: list[tuple[str, bytes]] = []
    # storage path → why its bytes never arrived. Carried all the way to the
    # README so a missing proof names itself instead of becoming a bare count.
    proof_failures: dict[str, str] = {}
    if receipt_tasks:
        await asyncio.gather(*(t for _, _, t in receipt_tasks), return_exceptions=True)

        for path, zip_filename, task in receipt_tasks:
            try:
                data, reason = task.result()
            except Exception as e:
                logger.warning("Proof-of-transaction fetch exception for %s: %s", path, e)
                data, reason = None, _FILE_GONE

            if data is None:
                proof_failures[path] = reason or _FILE_GONE
                continue

            receipt_files.append((zip_filename, data))

    # ── Build verified receipt map (only files actually fetched) ──────────
    fetched_filenames = {fn for fn, _ in receipt_files}
    verified_receipt_map: dict[int, str | list[str]] = {}
    for txn_id, fnames in receipt_name_map.items():
        if isinstance(fnames, list):
            verified = [fn for fn in fnames if fn in fetched_filenames]
            if verified:
                verified_receipt_map[txn_id] = verified if len(verified) > 1 else verified[0]
        elif fnames in fetched_filenames:
            verified_receipt_map[txn_id] = fnames

    # ── Compute each fetched proof's path inside the ZIP ─────────────────
    # Multi-proof bundles live in a per-transaction subfolder. This map is
    # the single source of truth shared by the XLSX hyperlinks, the HTML
    # report hrefs, and the ZIP writer — so a click target always matches
    # an actual archive entry.
    filename_to_subfolder: dict[str, str] = {}
    for txn_id in multi_proof_txns:
        fnames = receipt_name_map.get(txn_id)
        if not isinstance(fnames, list) or len(fnames) < 2:
            continue
        txn = txn_lookup.get(txn_id)
        date_str = txn.date_posted.strftime("%Y%m%d") if txn and txn.date_posted else "00000000"
        vendor_raw = vendors.get(txn_id, "Unknown")
        vname = _safe_filename(vendor_raw[0] if isinstance(vendor_raw, list) and vendor_raw else (vendor_raw if isinstance(vendor_raw, str) else "Unknown"))
        subfolder = f"{date_str}-{vname}-bundle"
        for fn in fnames:
            filename_to_subfolder[fn] = subfolder

    proof_rel_paths: dict[str, str] = {}
    for filename, _data in receipt_files:
        subfolder = filename_to_subfolder.get(filename)
        proof_rel_paths[filename] = (
            f"proof_of_transaction/{subfolder}/{filename}" if subfolder
            else f"proof_of_transaction/{filename}"
        )

    # ── Build XLSX ledger (Summary + per-account tabs + Transfers tab) ────
    xlsx_bytes = _build_xlsx(
        company_name, year, month, label,
        transactions, doc_paths, vendors,
        receipt_name_map=verified_receipt_map,
        transfer_txn_ids=transfer_txn_ids,
        transaction_links=transaction_links,
        proof_rel_paths=proof_rel_paths,
    )
    xlsx_filename = f"{safe_company}_{label}_Ledger.xlsx"

    # ── Build linked-in-month map (one side → every counterpart) ─────────
    # Only includes links where both endpoints are in this month's txn batch,
    # so index.html can render same-month "Linked" status as in-page anchors.
    # A list, not a single id: a charge refunded across several credits has
    # several counterparts, and a scalar map would keep only the last one — an
    # auditor following the binder would see part of the evidence.
    linked_in_month_map: dict[int, list[int]] = {}
    month_txn_ids = {t.id for t in transactions if t.id is not None}
    for link in (transaction_links or []):
        src_id = link.get("source_transaction_id")
        tgt_id = link.get("target_transaction_id")
        if src_id in month_txn_ids and tgt_id in month_txn_ids:
            linked_in_month_map.setdefault(src_id, []).append(tgt_id)
            linked_in_month_map.setdefault(tgt_id, []).append(src_id)

    # ── Fetch statement files (so each account's table can link to the
    # original uploaded statement document, letting the user validate the
    # generated table against the actual bank/wise statement) ────────────
    statement_files: list[tuple[str, bytes]] = []          # (zip_filename, bytes)
    statement_zip_map: dict[str, str] = {}                 # source_file → zip filename
    if statement_paths:
        # Restrict to source_files actually used by transactions in this batch
        used_source_files = {
            t.source_file for t in transactions if getattr(t, "source_file", None)
        }
        used_filenames_stmt: set[str] = set()
        statement_tasks: list[tuple[str, str, asyncio.Task]] = []
        for source_file, stored_path in statement_paths.items():
            if source_file not in used_source_files or not stored_path:
                continue
            base = _safe_filename(source_file)
            ext = Path(source_file).suffix or Path(stored_path).suffix or ""
            if ext and not base.lower().endswith(ext.lower()):
                base = f"{base}{ext}"
            zip_filename = base
            counter = 2
            while zip_filename in used_filenames_stmt:
                stem = base.rsplit('.', 1)[0]
                zip_filename = f"{stem}-{counter}{ext}" if ext else f"{stem}-{counter}"
                counter += 1
            used_filenames_stmt.add(zip_filename)

            task = asyncio.ensure_future(_fetch_receipt_bytes(stored_path, user_id))
            statement_tasks.append((source_file, zip_filename, task))

        if statement_tasks:
            await asyncio.gather(*(t for _, _, t in statement_tasks), return_exceptions=True)
            for source_file, zip_filename, task in statement_tasks:
                try:
                    data, _reason = task.result()
                except Exception as e:
                    logger.warning("Statement fetch exception for %s: %s", source_file, e)
                    data = None
                if data is None:
                    continue
                statement_files.append((zip_filename, data))
                statement_zip_map[source_file] = zip_filename

    # ── One tally, read by BOTH writers in this ZIP ───────────────────────
    # Counting proofs independently in README.txt and index.html lets one of
    # them report "Proofs of Transaction: 0" beside rows the other badges
    # "Linked". Both read this instead, derived from the proofs actually
    # written into proof_of_transaction/ below.
    proof_counts = binder_proof_counts(
        transactions, verified_receipt_map, proof_file_count=len(receipt_files),
    )

    # ── Build README (once the archived statement count is known) ─────────
    readme_text = _build_readme(
        company_name, label, year, month,
        len(transactions), len(receipt_files),
        statement_count=len(statement_zip_map),
        counts=proof_counts,
        missing_proofs=_missing_proof_rows(
            transactions, verified_receipt_map, doc_paths, proof_failures
        ),
    )

    # ── Generate HTML report ──────────────────────────────────────────────
    try:
        html_content = await generate_html_report(
            company_name, year, month, label, transactions, doc_paths, vendors,
            receipt_name_map=verified_receipt_map,
            linked_in_month_map=linked_in_month_map,
            statement_zip_map=statement_zip_map,
            transfer_txn_ids=transfer_txn_ids,
            proof_zip_paths=proof_rel_paths,
            proof_file_count=len(receipt_files),
        )
    except Exception as e:
        logger.warning("HTML report generation failed, continuing without it: %s", e)
        html_content = None

    # ── Assemble ZIP (UNENCRYPTED) ────────────────────────────────────────
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # HTML report first (easy to find after extraction)
        if html_content:
            zf.writestr("index.html", html_content)

        # XLSX ledger (includes the Transfers tab — no separate file)
        zf.writestr(xlsx_filename, xlsx_bytes)

        # Proofs of transaction — written at the exact paths the XLSX
        # hyperlinks and HTML hrefs point to (multi-proof bundles grouped
        # into per-transaction sub-folders).
        for filename, data in receipt_files:
            zf.writestr(proof_rel_paths[filename], data)

        # Original uploaded statement files — linked from each account section
        # in index.html so the user / auditor can verify the generated table
        # against the source document.
        for filename, data in statement_files:
            zf.writestr(f"statements/{filename}", data)

        # README
        zf.writestr("README.txt", readme_text)

    zip_bytes = zip_buf.getvalue()
    zip_buf.close()

    logger.info(
        "Generated record binder for %s %s: %d bytes, %d proofs of transaction, "
        "%d source statements, ZIP unencrypted",
        company_name, label, len(zip_bytes), len(receipt_files), len(statement_files),
    )
    return zip_bytes
