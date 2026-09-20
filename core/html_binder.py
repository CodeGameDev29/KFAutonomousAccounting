"""HTML report generation for the record binder ZIP.

Produces a self-contained index.html with embedded CSS, per-currency
executive summary, category P&L ("Where the money went") grouped by
ISED/GIFI section, reconciliation status, internal-transfer appendix,
per-account transaction tables, clickable receipt links, and
print-friendly CSS. No external network requests — works fully offline
after ZIP extraction.

ACCURACY RULES (mirror the Analytics page):
  * Amounts in different currencies are NEVER numerically summed into a
    single total. Every aggregate is computed and displayed per currency.
  * Internal transfers (transfer-like category or transfer-type link) are
    listed in the tables but excluded from all Money in / Money out / Net
    totals, and itemized in their own appendix so an auditor can see
    exactly what was excluded and why.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime
from decimal import Decimal
from html import escape as html_escape
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config.data_files import account_display, account_order
from config.ised_categories import (
    CATEGORY_BY_NAME,
    SECTIONS,
    UNCATEGORIZED_LABEL,
    remap_legacy,
)
from core.transfer_exclusion import is_transfer_category
from models.transaction import account_str

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Preferred display order for known account types. Any account key not listed
# here (custom LLM-parsed institutions, Amazon, etc.) is appended alphabetically
# after these, so PayPal, Amazon, BMO, etc. are never silently dropped.
# Both tables come from config/accounts.yaml; edit that file to rename an
# account or change the order it appears in.
ACCOUNT_ORDER_KEYS = account_order()
ACCOUNT_DISPLAY = account_display()

STATUS_LABELS = {
    "MATCHED": "Matched",
    "UNMATCHED": "Unmatched",
    # Self-evident row (bank fee, interest, transfer): needs no proof document,
    # but its money IS counted in every total on this page. Never label it
    # "Ignored"/"Excluded" — an accountant or an auditor would read that as
    # "this amount was left out of the books", which is false.
    "IGNORED": "No receipt needed",
    # A LINKED row is matched to its COUNTERPART TRANSACTION (the other side of
    # an internal transfer), not to a receipt — it has no file in
    # proof_of_transaction/. A bare "Linked" in an audit-facing document reads
    # as "substantiated", so a binder holding no proofs at all would still
    # look documented.
    "LINKED": "Linked to transfer",
    "PENDING_REVIEW": "Pending review",
    # The row isn't real (rejected at import). Its money is counted nowhere.
    "EXCLUDED": "Excluded — not counted",
}

# Section display order for "Where the money went". The four taxonomy
# sections come from config.ised_categories.SECTIONS; "uncategorized" is the
# NULL sentinel and always renders last.
_SECTION_ORDER: list[tuple[str, str]] = [(key, label) for key, label, _names in SECTIONS]
_SECTION_ORDER.append(("uncategorized", UNCATEGORIZED_LABEL))


def _proof_filenames(receipt_name_map: dict | None) -> set[str]:
    """Flatten a ``{txn_id: filename | [filenames]}`` map to a filename set."""
    names: set[str] = set()
    for value in (receipt_name_map or {}).values():
        if isinstance(value, list):
            names.update(v for v in value if v)
        elif value:
            names.add(value)
    return names


def binder_proof_counts(
    transactions: list,
    receipt_name_map: dict | None = None,
    proof_file_count: int | None = None,
) -> dict:
    """The binder's proof/reconciliation tally — ONE source of truth.

    Both writers inside a binder ZIP read this: ``index.html``'s
    Reconciliation Status section and ``README.txt``'s header block. Counting
    separately lets them disagree — a README reporting no proofs at all beside
    rows the page badges "Linked", in an audit-facing document with no
    ``proof_of_transaction/`` folder in the ZIP.

    ``receipt_name_map`` is the VERIFIED map (only proofs whose bytes were
    actually fetched and written into the ZIP), so ``with_proof`` can never
    exceed what an auditor can open. ``proof_file_count`` is the number of
    files written; it defaults to the distinct filenames in the map.

    Deliberately separate keys, because they are different claims:
      * ``with_proof`` — rows an auditor can substantiate from this ZIP.
      * ``matched_no_proof_file`` — rows the engine matched whose file could
        not be archived. A gap, surfaced rather than folded into "matched".
      * ``linked_to_transfer`` — rows matched to their counterpart
        transaction. Documented as a transfer, NOT by a receipt.
    """
    archived = _proof_filenames(receipt_name_map)
    if proof_file_count is None:
        proof_file_count = len(archived)

    def _has_proof(txn) -> bool:
        if txn.id is None:
            return False
        value = (receipt_name_map or {}).get(txn.id)
        if isinstance(value, list):
            return any(v in archived for v in value)
        return bool(value) and value in archived

    counts = {
        "transactions": len(transactions),
        "proof_files": proof_file_count,
        "with_proof": 0,
        "matched_no_proof_file": 0,
        "linked_to_transfer": 0,
        "pending_review": 0,
        "no_document": 0,
        "no_receipt_needed": 0,
        "excluded": 0,
    }

    for txn in transactions:
        status = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
        if _has_proof(txn):
            counts["with_proof"] += 1
        elif status == "MATCHED":
            counts["matched_no_proof_file"] += 1
        elif status == "LINKED":
            counts["linked_to_transfer"] += 1
        elif status == "PENDING_REVIEW":
            counts["pending_review"] += 1
        elif status == "IGNORED":
            counts["no_receipt_needed"] += 1
        elif status == "EXCLUDED":
            counts["excluded"] += 1
        else:
            counts["no_document"] += 1

    # "Needs a proof" excludes self-evident rows (bank fees, interest,
    # internal transfers) and rows rejected at import.
    reconcilable = (
        counts["transactions"] - counts["no_receipt_needed"] - counts["excluded"]
    )
    counts["reconcilable"] = reconcilable
    counts["substantiated_rate"] = (
        round(counts["with_proof"] / reconcilable * 100, 1) if reconcilable else 0.0
    )
    return counts


def _account_slug(key: str) -> str:
    """Build a URL-safe slug from an account key for anchor IDs."""
    slug = re.sub(r'[^a-z0-9]+', '-', key.lower()).strip('-')
    return slug or "account"


def _ordered_account_keys(keys) -> list:
    """Order account keys: known ones first (ACCOUNT_ORDER_KEYS), rest alphabetically."""
    seen = set()
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


def _format_currency(amount, currency="CAD"):
    """Format an amount with an explicit currency marker.

    CAD/USD get their conventional prefixes; every other ISO code renders
    as ``EUR 1,234.56`` so no currency is ever mislabeled as dollars.
    """
    if isinstance(amount, (int, float)):
        amount = Decimal(str(amount))
    sign = "" if amount >= 0 else "−"
    abs_val = abs(amount)
    cur = currency or "CAD"
    if cur == "CAD":
        return f"{sign}C${abs_val:,.2f}"
    if cur == "USD":
        return f"{sign}US${abs_val:,.2f}"
    return f"{sign}{cur} {abs_val:,.2f}"


def _url_encode_filename(s):
    """URL-encode a filename for use in href attributes."""
    return quote(str(s), safe="-_.~")


def _build_jinja_env() -> Environment:
    """Create a Jinja2 environment with custom filters."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["currency"] = _format_currency
    env.filters["urlencode"] = _url_encode_filename
    return env


def _txn_currency(t) -> str:
    return getattr(t, "currency", None) or "CAD"


def _currency_sort_key(totals: dict) -> tuple:
    """CAD first, USD second, then by gross activity descending."""
    cur = totals["currency"]
    activity = abs(totals["income"]) + abs(totals["expenses"])
    rank = 0 if cur == "CAD" else (1 if cur == "USD" else 2)
    return (rank, -activity, cur)


def _category_meta(raw_category: str | None) -> tuple[str, str, str | None]:
    """Resolve a stored category string to (display_name, section_key, gifi).

    Older category names are remapped to the canonical taxonomy for
    section/GIFI lookup but displayed with the name the row carries, so the
    binder never shows a category name the user didn't choose.
    """
    if not raw_category:
        return UNCATEGORIZED_LABEL, "uncategorized", None
    canonical = remap_legacy(raw_category)
    c = CATEGORY_BY_NAME.get(canonical) if canonical else None
    if c is None:
        return raw_category, "uncategorized", None
    return raw_category, c.section, c.gifi


def _vendor_labels(vendor_val) -> list[str]:
    """Normalize a vendors-map value (str | list[str] | None) to a list."""
    if isinstance(vendor_val, list):
        return [v for v in vendor_val if v]
    if isinstance(vendor_val, str) and vendor_val:
        return [vendor_val]
    return []


def _sum_currency_totals(txns, is_transfer) -> list[dict]:
    """Per-currency Money in / Money out / Net over non-transfer transactions."""
    by_cur: dict[str, dict] = {}
    for t in txns:
        if is_transfer(t):
            continue
        cur = _txn_currency(t)
        agg = by_cur.setdefault(cur, {
            "currency": cur,
            "income": Decimal("0"),
            "expenses": Decimal("0"),
            "net": Decimal("0"),
            "txn_count": 0,
        })
        amt = abs(Decimal(str(t.amount)))
        if t.transaction_type == "CREDIT":
            agg["income"] += amt
            agg["net"] += amt
        else:
            agg["expenses"] += amt
            agg["net"] -= amt
        agg["txn_count"] += 1
    return sorted(by_cur.values(), key=_currency_sort_key)


def _build_category_breakdown(txns, is_transfer) -> list[dict]:
    """Per-currency category P&L grouped by ISED section.

    Mirrors DatabasePg.get_category_breakdown semantics: transfers excluded,
    all other statuses included, NULL category → "Uncategorized".
    """
    # (currency, category) → aggregate
    agg: dict[tuple[str, str], dict] = {}
    for t in txns:
        if is_transfer(t):
            continue
        cur = _txn_currency(t)
        name, section, gifi = _category_meta(getattr(t, "category", None))
        row = agg.setdefault((cur, name), {
            "category": name,
            "section": section,
            "gifi": gifi,
            "currency": cur,
            "income": Decimal("0"),
            "expenses": Decimal("0"),
            "net": Decimal("0"),
            "txn_count": 0,
        })
        amt = abs(Decimal(str(t.amount)))
        if t.transaction_type == "CREDIT":
            row["income"] += amt
            row["net"] += amt
        else:
            row["expenses"] += amt
            row["net"] -= amt
        row["txn_count"] += 1

    # currency → section → rows
    by_cur: dict[str, dict[str, list[dict]]] = {}
    for row in agg.values():
        by_cur.setdefault(row["currency"], {}).setdefault(row["section"], []).append(row)

    breakdowns = []
    for cur, sections_map in by_cur.items():
        sections = []
        cur_income = Decimal("0")
        cur_expenses = Decimal("0")
        for key, label in _SECTION_ORDER:
            rows = sections_map.get(key)
            if not rows:
                continue
            rows.sort(key=lambda r: abs(r["net"]), reverse=True)
            s_income = sum(r["income"] for r in rows)
            s_expenses = sum(r["expenses"] for r in rows)
            sections.append({
                "key": key,
                "label": label,
                "rows": rows,
                "income": s_income,
                "expenses": s_expenses,
                "net": s_income - s_expenses,
            })
            cur_income += s_income
            cur_expenses += s_expenses
        breakdowns.append({
            "currency": cur,
            "sections": sections,
            "income": cur_income,
            "expenses": cur_expenses,
            "net": cur_income - cur_expenses,
        })
    breakdowns.sort(key=_currency_sort_key)
    return breakdowns


def _build_txn_row(
    txn,
    vendors: dict,
    receipt_name_map: dict,
    linked_in_month_map: dict,
    is_transfer,
    proof_zip_paths: dict | None = None,
) -> dict:
    """Precompute one display row so the template stays logic-free."""
    status = txn.status.value if hasattr(txn.status, "value") else str(txn.status)
    category_name, _section, _gifi = _category_meta(getattr(txn, "category", None))
    vendor_list = _vendor_labels(vendors.get(txn.id)) if txn.id is not None else []

    # Proof-of-transaction cell states, in priority order.
    proofs: list[dict] = []
    proof_state = "none"
    raw_names = receipt_name_map.get(txn.id) if txn.id is not None else None
    if raw_names:
        names = raw_names if isinstance(raw_names, list) else [raw_names]
        for i, fn in enumerate(names):
            label = vendor_list[i] if i < len(vendor_list) else Path(fn).name
            # Multi-proof bundle files live in a sub-folder inside the ZIP —
            # href must use the actual archive path, not the bare filename.
            rel = (proof_zip_paths or {}).get(fn, f"proof_of_transaction/{fn}")
            proofs.append({"href": quote(rel, safe="/-_.~"),
                           "label": label[:40]})
        proof_state = "linked"
    elif status == "MATCHED":
        proof_state = "not_archived"
    elif status == "IGNORED":
        proof_state = "na"

    amount = Decimal(str(txn.amount))
    is_credit = txn.transaction_type == "CREDIT"

    return {
        "id": txn.id,
        "date": txn.date_posted.isoformat() if txn.date_posted else "",
        "description": (txn.description or "")[:80],
        "vendor": vendor_list[0][:40] if vendor_list else "",
        "category": category_name,
        "is_uncategorized": category_name == UNCATEGORIZED_LABEL,
        "amount_display": _format_currency(abs(amount), _txn_currency(txn)),
        "sign": "+" if is_credit else "−",
        "is_credit": is_credit,
        "currency": _txn_currency(txn),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status.title()),
        # Every same-month counterpart, not just one: a split refund group
        # has several, and the binder is the artifact an auditor reads.
        "linked_targets": list(linked_in_month_map.get(txn.id) or []) if txn.id is not None else [],
        "proof_state": proof_state,
        "proofs": proofs,
        "note": getattr(txn, "note", None) or "",
        "is_transfer": is_transfer(txn),
    }


async def generate_html_report(
    company_name: str,
    year: int,
    month: int,
    label: str,
    transactions: list,
    doc_paths: dict[int, str | list[str]],
    vendors: dict[int, str | list[str]],
    receipt_name_map: dict[int, str | list[str]] | None = None,
    linked_in_month_map: dict[int, list[int]] | None = None,
    statement_zip_map: dict[str, str] | None = None,
    transfer_txn_ids: set[int] | None = None,
    proof_zip_paths: dict[str, str] | None = None,
    proof_file_count: int | None = None,
) -> str:
    """Generate a self-contained HTML report for the record binder.

    Args:
        company_name: Business name for headers.
        year: Report year.
        month: Report month (1-12).
        label: Month label (e.g. "Mar 2026").
        transactions: List of Transaction model instances.
        doc_paths: Mapping of transaction_id -> stored receipt path.
        vendors: Mapping of transaction_id -> vendor display name.
        receipt_name_map: Mapping of transaction_id -> filename in ZIP
            (proof_of_transaction/ folder). When provided, HTML links
            use these filenames instead of deriving from doc_paths.
        statement_zip_map: Optional mapping `source_file` → relative path
            inside the ZIP so each account's section can link to the
            original uploaded statement(s).

    Returns:
        Complete HTML string ready to write as index.html.
    """
    if not transactions:
        return _build_empty_html(company_name, year, month)

    receipt_name_map = receipt_name_map or {}
    linked_in_month_map = linked_in_month_map or {}
    statement_zip_map = statement_zip_map or {}

    def _is_transfer(t) -> bool:
        if transfer_txn_ids and t.id is not None and t.id in transfer_txn_ids:
            return True
        return is_transfer_category(getattr(t, "category", None))

    # ── Per-currency executive summary + category P&L ─────────────────────
    currency_totals = _sum_currency_totals(transactions, _is_transfer)
    category_breakdowns = _build_category_breakdown(transactions, _is_transfer)

    # ── Reconciliation status ─────────────────────────────────────────────
    # Counted by binder_proof_counts so this page and README.txt can never
    # disagree about how many proofs the ZIP actually holds. A LINKED row is
    # documented by its counterpart transaction, never by a receipt, and is
    # reported on its own line rather than lumped in with matched rows.
    counts = binder_proof_counts(transactions, receipt_name_map, proof_file_count)

    # ── Internal transfers appendix ────────────────────────────────────────
    transfer_rows = [
        {
            "id": t.id,
            "date": t.date_posted.isoformat() if t.date_posted else "",
            "account": ACCOUNT_DISPLAY.get(account_str(t.account) or "", account_str(t.account) or "Unknown"),
            "description": (t.description or "")[:80],
            "amount_display": _format_currency(abs(Decimal(str(t.amount))), _txn_currency(t)),
            "sign": "+" if t.transaction_type == "CREDIT" else "−",
            "is_credit": t.transaction_type == "CREDIT",
            "category": _category_meta(getattr(t, "category", None))[0],
            "linked_targets": list(linked_in_month_map.get(t.id) or []) if t.id is not None else [],
        }
        for t in sorted(transactions, key=lambda t: (t.date_posted, t.id or 0))
        if _is_transfer(t)
    ]

    # ── Per-account sections ───────────────────────────────────────────────
    txn_by_account: dict[str, list] = {}
    for txn in transactions:
        key = account_str(txn.account) or "Unknown"
        txn_by_account.setdefault(key, []).append(txn)

    account_summaries = []
    for acct_key in _ordered_account_keys(txn_by_account.keys()):
        acct_txns = txn_by_account.get(acct_key, [])
        if not acct_txns:
            continue

        acct_currency_totals = _sum_currency_totals(acct_txns, _is_transfer)
        transfer_count = sum(1 for t in acct_txns if _is_transfer(t))

        # Source statement links for this account (first-seen order).
        seen_sources: set[str] = set()
        source_files_for_acct: list[dict[str, str]] = []
        for t in acct_txns:
            sf = getattr(t, "source_file", None)
            if not sf or sf in seen_sources:
                continue
            seen_sources.add(sf)
            zip_filename = statement_zip_map.get(sf)
            source_files_for_acct.append({
                "source_file": sf,
                "zip_path": f"statements/{zip_filename}" if zip_filename else "",
                "available": bool(zip_filename),
            })

        rows = [
            _build_txn_row(t, vendors, receipt_name_map, linked_in_month_map, _is_transfer,
                           proof_zip_paths=proof_zip_paths)
            for t in sorted(acct_txns, key=lambda t: (t.date_posted, t.id or 0))
        ]

        account_summaries.append({
            "account_name": ACCOUNT_DISPLAY.get(acct_key, acct_key),
            "account_code": _account_slug(acct_key),
            "transaction_count": len(acct_txns),
            "transfer_count": transfer_count,
            "currency_totals": acct_currency_totals,
            "rows": rows,
            "source_files": source_files_for_acct,
        })

    context = {
        "company_name": company_name,
        "year": year,
        "month": month,
        "label": label,
        "month_name": calendar.month_name[month],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_transactions": len(transactions),
        "currency_totals": currency_totals,
        "multi_currency": len(currency_totals) > 1,
        "category_breakdowns": category_breakdowns,
        "reconciliation": counts,
        "transfer_rows": transfer_rows,
        "accounts": account_summaries,
    }

    env = _build_jinja_env()
    template = env.get_template("binder.html")
    html = template.render(**context)

    logger.info(
        "Generated HTML report for %s %s: %d accounts, %d transactions, %d currencies",
        company_name, label, len(account_summaries), len(transactions), len(currency_totals),
    )
    return html


def _build_empty_html(company_name: str, year: int, month: int) -> str:
    """Return a minimal HTML page for months with no transactions."""
    month_name = calendar.month_name[month]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    # company_name is user-controlled (profile input) — escape to prevent
    # stored XSS in the generated binder HTML (CWE-79).
    safe_company = html_escape(str(company_name), quote=True)
    safe_month = html_escape(month_name, quote=True)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{safe_company} — {safe_month} {year} Record Binder</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
           max-width: 800px; margin: 2rem auto; padding: 0 1rem; color: #1a1a2e; }}
    h1 {{ color: #0f3460; border-bottom: 3px solid #0f3460; padding-bottom: 0.5rem; }}
  </style>
</head>
<body>
  <h1>{safe_company}</h1>
  <p>Monthly record binder — {safe_month} {year}</p>
  <p>No transactions recorded for this period.</p>
  <footer style="margin-top:3rem;padding-top:1rem;border-top:1px solid #dee2e6;color:#6c757d;font-size:0.85rem;">
    <p>Generated by Autonomous Accounting | {generated}</p>
  </footer>
</body>
</html>"""
