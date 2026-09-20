"""PDF report generation for monthly accounting reports.

Generates print-ready PDF reports with ReportLab. Each report covers one
month and groups transactions by account type.
"""

from __future__ import annotations

import calendar
import io
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from config.data_files import account_display, account_order
from core.html_binder import (
    STATUS_LABELS,
    _build_category_breakdown,
    _sum_currency_totals,
)
from core.transfer_exclusion import is_transfer_category
from models.transaction import TransactionStatus, account_str

logger = logging.getLogger(__name__)

# ─── Colour palette ──────────────────────────────────────────────────────────
BRAND_DARK = colors.HexColor("#1a1a2e")
BRAND_ACCENT = colors.HexColor("#16213e")
BRAND_BLUE = colors.HexColor("#0f3460")
HEADER_BG = colors.HexColor("#0f3460")
HEADER_FG = colors.white
ROW_EVEN = colors.HexColor("#f8f9fa")
ROW_ODD = colors.white
INCOME_GREEN = colors.HexColor("#28a745")
EXPENSE_RED = colors.HexColor("#dc3545")
BORDER_COLOR = colors.HexColor("#dee2e6")
LIGHT_GRAY = colors.HexColor("#6c757d")


# ─── Account display names (string keys — txn.account is a free-form string) ─
# Both tables come from config/accounts.yaml; edit that file to rename an
# account or change the order it appears in.
ACCOUNT_DISPLAY = account_display()

# Preferred display order. Any account key not listed here (custom LLM-parsed
# institutions like BMO, etc.) is appended alphabetically so no
# transactions get silently dropped from the PDF.
ACCOUNT_ORDER_KEYS = account_order()


def _ordered_account_keys(keys) -> list:
    """Order account keys: known ones first, remainder alphabetically."""
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


def _build_styles() -> dict[str, ParagraphStyle]:
    """Build a dict of reusable paragraph styles."""
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=base["Title"],
            fontSize=22,
            leading=26,
            textColor=BRAND_DARK,
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle",
            parent=base["Normal"],
            fontSize=12,
            leading=16,
            textColor=LIGHT_GRAY,
            spaceAfter=16,
        ),
        "section": ParagraphStyle(
            "SectionHeader",
            parent=base["Heading2"],
            fontSize=14,
            leading=18,
            textColor=BRAND_BLUE,
            spaceBefore=18,
            spaceAfter=8,
        ),
        "body": ParagraphStyle(
            "BodyText",
            parent=base["Normal"],
            fontSize=9,
            leading=12,
        ),
        "summary_label": ParagraphStyle(
            "SummaryLabel",
            parent=base["Normal"],
            fontSize=10,
            leading=14,
            textColor=LIGHT_GRAY,
        ),
        "summary_value": ParagraphStyle(
            "SummaryValue",
            parent=base["Normal"],
            fontSize=12,
            leading=16,
            textColor=BRAND_DARK,
        ),
        "cell": ParagraphStyle(
            "CellText",
            parent=base["Normal"],
            fontSize=8,
            leading=10,
            wordWrap="LTR",
        ),
        "cell_right": ParagraphStyle(
            "CellRight",
            parent=base["Normal"],
            fontSize=8,
            leading=10,
            alignment=TA_RIGHT,
        ),
        "cell_center": ParagraphStyle(
            "CellCenter",
            parent=base["Normal"],
            fontSize=8,
            leading=10,
            alignment=TA_CENTER,
        ),
        "footer": ParagraphStyle(
            "FooterStyle",
            parent=base["Normal"],
            fontSize=7,
            leading=9,
            textColor=LIGHT_GRAY,
            alignment=TA_CENTER,
        ),
    }


def _format_amount(amount: Decimal, currency: str) -> str:
    """Format a Decimal amount with currency prefix."""
    sign = "-" if amount < 0 else ""
    abs_val = abs(amount)
    if currency == "CAD":
        return f"{sign}C${abs_val:,.2f}"
    elif currency == "USD":
        return f"{sign}US${abs_val:,.2f}"
    else:
        return f"{sign}{currency} {abs_val:,.2f}"


def _receipt_label(
    txn_id: int | None,
    status: TransactionStatus,
    doc_paths: dict[int, str | list[str]],
    vendors: dict[int, str | list[str]],
) -> str:
    """Return receipt column label for a transaction."""
    if status == TransactionStatus.IGNORED:
        return "N/A"
    if txn_id is not None and txn_id in doc_paths:
        vendor_val = vendors.get(txn_id, "Matched")
        if isinstance(vendor_val, list):
            primary = vendor_val[0] if vendor_val else "Matched"
            if len(vendor_val) > 1:
                return f"{primary} +{len(vendor_val)-1} more"
            return primary
        return vendor_val
    if status == TransactionStatus.LINKED:
        # A linked internal transfer is documented by its counterpart
        # transaction, not a receipt — "Missing" here would be misleading.
        return "Linked transfer"
    if status == TransactionStatus.PENDING_REVIEW:
        return "Pending review"
    return "Missing"


def _receipt_color(label: str) -> colors.Color:
    """Return text color based on receipt label."""
    if label == "Missing":
        return EXPENSE_RED
    if label in ("N/A", "Linked transfer", "Pending review"):
        return LIGHT_GRAY
    return INCOME_GREEN


def _build_summary_table(
    currency_totals: list[dict],
    styles: dict[str, ParagraphStyle],
) -> Table:
    """Build the per-currency Money in / Money out / Net summary table.

    One row per currency — amounts in different currencies are never
    numerically summed into a single figure.
    """
    header = [
        Paragraph("<b>Currency</b>", styles["cell"]),
        Paragraph("<b>Money in</b>", styles["cell_right"]),
        Paragraph("<b>Money out</b>", styles["cell_right"]),
        Paragraph("<b>Net</b>", styles["cell_right"]),
        Paragraph("<b>Transactions</b>", styles["cell_right"]),
    ]
    rows: list[list] = [header]
    for ct in currency_totals:
        cur = ct["currency"]
        net = ct["net"]
        net_color = "#28a745" if net >= 0 else "#dc3545"
        rows.append([
            Paragraph(cur, styles["cell"]),
            Paragraph(f"<font color='#28a745'>{_format_amount(ct['income'], cur)}</font>", styles["cell_right"]),
            Paragraph(f"<font color='#dc3545'>{_format_amount(ct['expenses'], cur)}</font>", styles["cell_right"]),
            Paragraph(f"<font color='{net_color}'>{_format_amount(net, cur)}</font>", styles["cell_right"]),
            Paragraph(str(ct["txn_count"]), styles["cell_right"]),
        ])

    col_widths = [1.0 * inch, 1.6 * inch, 1.6 * inch, 1.6 * inch, 1.2 * inch]
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, 0), 1, HEADER_BG),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, BORDER_COLOR),
    ]))
    return table


def _build_category_table(
    breakdown: dict,
    styles: dict[str, ParagraphStyle],
) -> Table:
    """Category P&L table for one currency, grouped by ISED/GIFI section."""
    cur = breakdown["currency"]
    header = [
        Paragraph("<b>Category</b>", styles["cell"]),
        Paragraph("<b>GIFI</b>", styles["cell_center"]),
        Paragraph("<b>Txns</b>", styles["cell_right"]),
        Paragraph("<b>Money in</b>", styles["cell_right"]),
        Paragraph("<b>Money out</b>", styles["cell_right"]),
        Paragraph("<b>Net</b>", styles["cell_right"]),
    ]
    rows: list[list] = [header]
    section_row_idxs: list[int] = []
    total_row_idx: int | None = None

    def _amt(v, color) -> Paragraph:
        if not v:
            return Paragraph("<font color='#6c757d'>—</font>", styles["cell_right"])
        return Paragraph(f"<font color='{color}'>{_format_amount(v, cur)}</font>", styles["cell_right"])

    for sec in breakdown["sections"]:
        section_row_idxs.append(len(rows))
        rows.append([
            Paragraph(f"<b>{sec['label'].upper()}</b>", styles["cell"]),
            "", "", "", "", "",
        ])
        for r in sec["rows"]:
            net_color = "#28a745" if r["net"] >= 0 else "#dc3545"
            rows.append([
                Paragraph(str(r["category"]), styles["cell"]),
                Paragraph(r["gifi"] or "", styles["cell_center"]),
                Paragraph(str(r["txn_count"]), styles["cell_right"]),
                _amt(r["income"], "#28a745"),
                _amt(r["expenses"], "#dc3545"),
                Paragraph(f"<font color='{net_color}'>{_format_amount(r['net'], cur)}</font>", styles["cell_right"]),
            ])

    total_row_idx = len(rows)
    net_color = "#28a745" if breakdown["net"] >= 0 else "#dc3545"
    rows.append([
        Paragraph("<b>Total</b>", styles["cell"]),
        "", "",
        Paragraph(f"<b><font color='#28a745'>{_format_amount(breakdown['income'], cur)}</font></b>", styles["cell_right"]),
        Paragraph(f"<b><font color='#dc3545'>{_format_amount(breakdown['expenses'], cur)}</font></b>", styles["cell_right"]),
        Paragraph(f"<b><font color='{net_color}'>{_format_amount(breakdown['net'], cur)}</font></b>", styles["cell_right"]),
    ])

    col_widths = [2.6 * inch, 0.75 * inch, 0.55 * inch, 1.15 * inch, 1.15 * inch, 1.15 * inch]
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    cmds: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, 0), 1, HEADER_BG),
    ]
    for idx in section_row_idxs:
        cmds.append(("BACKGROUND", (0, idx), (-1, idx), colors.HexColor("#eef2f7")))
        cmds.append(("SPAN", (0, idx), (-1, idx)))
        cmds.append(("LINEABOVE", (0, idx), (-1, idx), 0.75, BORDER_COLOR))
    if total_row_idx is not None:
        cmds.append(("LINEABOVE", (0, total_row_idx), (-1, total_row_idx), 1, HEADER_BG))
        cmds.append(("BACKGROUND", (0, total_row_idx), (-1, total_row_idx), colors.HexColor("#f0f3f7")))
    table.setStyle(TableStyle(cmds))
    return table


def _build_txn_table(
    transactions: list,
    doc_paths: dict[int, str | list[str]],
    vendors: dict[int, str | list[str]],
    styles: dict[str, ParagraphStyle],
    is_transfer=None,
) -> Table | None:
    """Build a transaction table for an account group. Returns None if empty."""
    if not transactions:
        return None

    # Sort by date (id tiebreak — same order as the web UI table)
    sorted_txns = sorted(transactions, key=lambda t: (t.date_posted, t.id or 0))

    # Header row
    headers = ["Date", "Description", "Category", "Amount", "Cur", "Status", "Proof of Transaction"]
    header_row = [Paragraph(f"<b>{h}</b>", styles["cell"]) for h in headers]

    rows = [header_row]
    for txn in sorted_txns:
        receipt_text = _receipt_label(txn.id, txn.status, doc_paths, vendors)
        receipt_clr = _receipt_color(receipt_text)

        # Format amount with color. Sign comes from the transaction TYPE and
        # the value is abs()'d — stored sign conventions vary by ingest path,
        # so deriving the sign from the amount can double-sign or flip rows.
        # Internal transfers render gray with a marker: listed, not totalled.
        is_txfr = bool(is_transfer and is_transfer(txn))
        abs_amt = abs(txn.amount)
        sign = "+" if txn.transaction_type == "CREDIT" else "-"
        if is_txfr:
            color = "#6c757d"
            suffix = " *"
        else:
            color = "#28a745" if txn.transaction_type == "CREDIT" else "#dc3545"
            suffix = ""
        amt_str = f"<font color='{color}'>{sign}{_format_amount(abs_amt, txn.currency)}{suffix}</font>"

        # Status display
        status_map = {
            TransactionStatus.MATCHED: "<font color='#28a745'>Matched</font>",
            TransactionStatus.UNMATCHED: "<font color='#dc3545'>Unmatched</font>",
            TransactionStatus.IGNORED: "<font color='#28a745'>No receipt needed</font>",
            TransactionStatus.EXCLUDED: "<font color='#6c757d'>Excluded</font>",
            # "Linked" alone reads as "substantiated" in an audit-facing
            # document; this row is matched to its transfer counterpart, not
            # to a receipt (same wording as index.html and the XLSX).
            TransactionStatus.LINKED: "<font color='#0c5460'>Linked to transfer</font>",
            TransactionStatus.PENDING_REVIEW: "<font color='#856404'>Pending review</font>",
        }
        status_value = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
        status_str = status_map.get(txn.status, STATUS_LABELS.get(status_value, status_value))

        receipt_html = f"<font color='{receipt_clr.hexval()}'>{receipt_text}</font>"

        category_label = getattr(txn, "category", None) or "—"

        desc = str(txn.description)
        if len(desc) > 110:
            desc = desc[:110] + "…"
        row = [
            Paragraph(txn.date_posted.strftime("%Y-%m-%d"), styles["cell"]),
            Paragraph(desc, styles["cell"]),
            Paragraph(str(category_label), styles["cell"]),
            Paragraph(amt_str, styles["cell_right"]),
            Paragraph(txn.currency, styles["cell_center"]),
            Paragraph(status_str, styles["cell_center"]),
            Paragraph(receipt_html, styles["cell"]),
        ]
        rows.append(row)

    # Column widths: Date, Description, Category, Amount, Cur, Status, Proof
    col_widths = [0.75 * inch, 1.85 * inch, 1.05 * inch, 1.0 * inch, 0.45 * inch, 0.75 * inch, 1.45 * inch]

    table = Table(rows, colWidths=col_widths, repeatRows=1)

    # Table styling
    style_commands: list[tuple[Any, ...]] = [
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), HEADER_FG),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        # Body
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        # Grid
        ("LINEBELOW", (0, 0), (-1, 0), 1, HEADER_BG),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, BORDER_COLOR),
        ("LINEAFTER", (0, 0), (-2, -1), 0.25, BORDER_COLOR),
    ]

    # Alternating row colours
    for i in range(1, len(rows)):
        bg = ROW_EVEN if i % 2 == 0 else ROW_ODD
        style_commands.append(("BACKGROUND", (0, i), (-1, i), bg))

    table.setStyle(TableStyle(style_commands))
    return table


def _footer(canvas, doc):
    """Draw page footer with page number and branding."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(LIGHT_GRAY)
    page_text = f"Page {doc.page}"
    canvas.drawCentredString(letter[0] / 2, 0.4 * inch, page_text)
    canvas.drawString(
        0.75 * inch, 0.4 * inch, "Generated by Autonomous Accounting"
    )
    canvas.drawRightString(
        letter[0] - 0.75 * inch,
        0.4 * inch,
        datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    canvas.restoreState()


def generate_month_pdf(
    company_name: str,
    year: int,
    month: int,
    label: str,
    transactions: list,
    doc_paths: dict[int, str | list[str]],
    vendors: dict[int, str | list[str]],
    transfer_txn_ids: set[int] | None = None,
) -> bytes:
    """Generate a professional PDF report for a single month.

    Args:
        company_name: Business name for the header.
        year: Report year (e.g. 2026).
        month: Report month (1-12).
        label: Month label (e.g. "Mar2026").
        transactions: List of Transaction model instances for the month.
        doc_paths: Mapping of transaction_id -> stored receipt path.
        vendors: Mapping of transaction_id -> vendor display name.

    Returns:
        PDF file content as bytes.
    """
    buf = io.BytesIO()
    styles = _build_styles()

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=0.6 * inch,
        bottomMargin=0.7 * inch,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        title=f"{company_name} — {label} Report",
        author="Autonomous Accounting",
    )

    story: list[Any] = []

    # ── Header ────────────────────────────────────────────────────────────────
    month_name = calendar.month_name[month]
    story.append(Paragraph(company_name, styles["title"]))
    story.append(Paragraph(
        f"Monthly Financial Report — {month_name} {year}",
        styles["subtitle"],
    ))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%B %d, %Y at %H:%M')}",
        styles["body"],
    ))
    story.append(Spacer(1, 12))

    # ── Summary ───────────────────────────────────────────────────────────────
    # Internal transfers (linked USD<->CAD moves, credit-card payments,
    # transfer-like categories) are listed in account tables but excluded
    # from income/expense totals to avoid double-counting. Totals are
    # computed and displayed PER CURRENCY — never numerically mixed.
    def _is_transfer(t) -> bool:
        if transfer_txn_ids and t.id is not None and t.id in transfer_txn_ids:
            return True
        return is_transfer_category(getattr(t, "category", None))

    currency_totals = _sum_currency_totals(transactions, _is_transfer)
    transfer_count = sum(1 for t in transactions if _is_transfer(t))

    status_counts: dict[str, int] = {}
    for txn in transactions:
        s = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
        status_counts[s] = status_counts.get(s, 0) + 1
    matched_count = status_counts.get("MATCHED", 0)
    linked_count = status_counts.get("LINKED", 0)
    ignored_count = status_counts.get("IGNORED", 0)
    reconcilable = len(transactions) - ignored_count
    documented = matched_count + linked_count
    documented_rate = (documented / reconcilable * 100) if reconcilable else 0.0

    story.append(Paragraph("Summary", styles["section"]))
    if currency_totals:
        story.append(_build_summary_table(currency_totals, styles))
    else:
        story.append(Paragraph(
            "Every transaction this month is an internal transfer between "
            "your own accounts — no income or spending to total.",
            styles["body"],
        ))
    story.append(Spacer(1, 8))

    # Supplementary summary lines
    story.append(Paragraph(
        f"<font color='#6c757d'>"
        f"{matched_count} matched to a proof document · {linked_count} linked transfers · "
        f"{status_counts.get('PENDING_REVIEW', 0)} pending review · "
        f"{status_counts.get('UNMATCHED', 0)} unmatched · "
        f"{ignored_count} need no receipt (amounts still counted) — "
        f"{documented_rate:.1f}% documented ({documented} of {reconcilable} reconcilable)"
        f"</font>",
        styles["body"],
    ))
    if transfer_count:
        story.append(Paragraph(
            f"<font color='#6c757d'>Internal transfers between your own accounts "
            f"({transfer_count} transactions) are listed in the tables (marked&nbsp;*) but excluded "
            f"from all totals and from the transaction counts above to avoid double-counting.</font>",
            styles["body"],
        ))
    if len(currency_totals) > 1:
        story.append(Paragraph(
            "<font color='#6c757d'>Currencies are totalled separately — amounts in "
            "different currencies are never added together in this report.</font>",
            styles["body"],
        ))
    story.append(Spacer(1, 16))

    # ── Where the money went (category P&L per currency, ISED/GIFI grouped) ──
    category_breakdowns = _build_category_breakdown(transactions, _is_transfer)
    if category_breakdowns:
        story.append(Paragraph("Where the Money Went", styles["section"]))
        story.append(Paragraph(
            "<font color='#6c757d'>Transactions grouped by category following the "
            "ISED / CRA GIFI income-statement structure.</font>",
            styles["body"],
        ))
        story.append(Spacer(1, 6))
        for bd in category_breakdowns:
            # Keep the currency label glued to its table so a page break
            # never orphans a lone "USD" heading at the bottom of a page.
            block: list[Any] = []
            if len(category_breakdowns) > 1:
                block.append(Paragraph(f"{bd['currency']}", styles["summary_label"]))
                block.append(Spacer(1, 2))
            block.append(_build_category_table(bd, styles))
            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))
    story.append(Spacer(1, 6))

    # ── Transaction tables by account ─────────────────────────────────────────
    # Group by the free-form string key (txn.account is not enum-bound) so
    # PayPal, Amazon, and any LLM-parsed institution name all render their
    # own section.
    txn_by_account: dict[str, list] = {}
    for txn in transactions:
        key = account_str(txn.account) or "Unknown"
        txn_by_account.setdefault(key, []).append(txn)

    for acct_key in _ordered_account_keys(txn_by_account.keys()):
        acct_txns = txn_by_account.get(acct_key)
        if not acct_txns:
            continue

        display_name = ACCOUNT_DISPLAY.get(acct_key, acct_key)

        # Collect the heading + per-currency subtotals into one block that is
        # kept together with the table, so an account heading is never
        # stranded alone at the bottom of a page.
        section_block: list[Any] = []
        section_block.append(Paragraph(
            f"{display_name} "
            f"<font size='9' color='#6c757d'>({len(acct_txns)} transactions)</font>",
            styles["section"],
        ))

        # Account-level sub-summary — one line per currency in the account
        # (a Wise account can hold USD/EUR/CAD; never sum across currencies
        # or label a mixed sum with whichever currency happens to come first).
        acct_currency_totals = _sum_currency_totals(acct_txns, _is_transfer)
        for ct in acct_currency_totals:
            cur = ct["currency"]
            prefix = f"<b>{cur}</b>&nbsp;&nbsp;" if len(acct_currency_totals) > 1 else ""
            section_block.append(Paragraph(
                f"{prefix}"
                f"<font color='#28a745'>Money in: {_format_amount(ct['income'], cur)}</font>"
                f"&nbsp;&nbsp;|&nbsp;&nbsp;"
                f"<font color='#dc3545'>Money out: {_format_amount(ct['expenses'], cur)}</font>"
                f"&nbsp;&nbsp;|&nbsp;&nbsp;"
                f"Net: {_format_amount(ct['net'], cur)}",
                styles["body"],
            ))
        if not acct_currency_totals:
            section_block.append(Paragraph(
                "<font color='#6c757d'>All transactions in this account are "
                "internal transfers — no income/expense subtotals.</font>",
                styles["body"],
            ))
        section_block.append(Spacer(1, 6))

        table = _build_txn_table(acct_txns, doc_paths, vendors, styles, is_transfer=_is_transfer)
        if table is not None:
            section_block.append(table)
            if any(_is_transfer(t) for t in acct_txns):
                section_block.append(Paragraph(
                    "<font color='#6c757d' size='7'>* internal transfer — listed for "
                    "auditability, excluded from all totals.</font>",
                    styles["body"],
                ))
        story.append(KeepTogether(section_block))
        story.append(Spacer(1, 12))

    # If no transactions at all
    if not transactions:
        story.append(Paragraph(
            "No transactions recorded for this period.",
            styles["body"],
        ))

    # ── Build ─────────────────────────────────────────────────────────────────
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    pdf_bytes = buf.getvalue()
    buf.close()

    logger.info(
        "Generated PDF report for %s %s: %d bytes, %d transactions",
        company_name, label, len(pdf_bytes), len(transactions),
    )
    return pdf_bytes
