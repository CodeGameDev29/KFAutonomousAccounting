"""Reports API — monthly/yearly reports, PDF export, ZIP record binder, share links, QBO/Xero CSV."""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from config.data_files import account_order
from config.settings import MONTH_ABBREVS
from core.transfer_exclusion import (
    countable_sql,
    is_transfer_category,
    transfer_exclusion_sql,
)
from db.database_pg import DatabasePg
from models.transaction import account_str
from server.api.link_view import build_counterpart_index, linked_summaries
from server.auth import AuthUser, get_current_user, get_current_user_header_or_query
from server.deps import get_db, get_db_header_or_query


def _sanitize_filename(name: str) -> str:
    """Sanitize a string for safe use in Content-Disposition headers (CWE-113).

    Replaces spaces with underscores so the resulting filename has no
    whitespace — this avoids Chrome's quirky handling of quoted filenames
    with spaces in ``Content-Disposition`` headers, which can cause some
    download handlers to fall back to UUID filenames.
    """
    cleaned = re.sub(r'[^a-zA-Z0-9 _\-.]', '', name) or "Company"
    return cleaned.replace(" ", "_")


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])


def _month_label(year: int, month: int) -> str:
    return f"{MONTH_ABBREVS[month]} {year}"


def _flatten_first(d: dict[int, list[str]]) -> dict[int, str]:
    """Take first element from multi-proof lists for backward compat."""
    return {k: v[0] for k, v in d.items() if v}


def _doc_tax_by_txn(db: DatabasePg) -> dict[int, dict]:
    """Per-transaction GST/HST evidence from the linked source documents.

    Returns ``{transaction_id: {"has_document": True, "gst": Decimal,
    "hst": Decimal}}`` summed over every approved match on that transaction
    (a bulk charge can be substantiated by several invoices).

    This is the ONLY input that lets an export assert a GST input tax credit
    — see ``core/gst_tax_code.py``. A transaction absent from this map has no
    document, so no ITC can be claimed for it.
    """
    from decimal import Decimal

    # documents.tax_* are TEXT decimal strings from the extraction pipeline, so
    # reuse the regex-guarded cast the tax summary already uses: a malformed
    # value degrades to 0 (no ITC claimed) instead of failing the export.
    gst_sql = DatabasePg._TAX_NUM_SQL.format(col="tax_gst")
    hst_sql = DatabasePg._TAX_NUM_SQL.format(col="tax_hst")

    out: dict[int, dict] = {}
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT rm.transaction_id, {gst_sql} AS gst, {hst_sql} AS hst
                   FROM reconciliation_matches rm
                   JOIN documents d ON rm.document_id = d.id
                   WHERE rm.user_id = %s
                     AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')""",
                (db.user_id,),
            )
            for txn_id, gst, hst in cur.fetchall():
                entry = out.setdefault(
                    txn_id,
                    {"has_document": True, "gst": Decimal("0"), "hst": Decimal("0")},
                )
                entry["gst"] += Decimal(str(gst or 0))
                entry["hst"] += Decimal(str(hst or 0))
    return out


# Xero and QBO both take a bank feed's currency from the account the file is
# imported into, never from a row. A month that spans a CAD account and a USD
# account therefore ships as one file per currency; when there are two or more
# they are bundled into a ZIP so a single click still gets everything.
_EXPORT_MANIFEST_HEADER = """{company} — {label} — {package} export
{rule}

This month holds transactions in more than one currency. {package} reads a
bank-statement CSV's currency from the bank account you import it into, not
from the rows, so a single mixed file would book every foreign amount as CAD.
Each file below is therefore one currency, named with its ISO code, and the
amounts inside it are native — nothing has been converted.

Import each file into the {package} bank account that is denominated in that
currency:

{files}
Autonomous Accounting does not record a transaction-level FX rate, so it does
not convert these amounts. Converting them is your accountant's call, using
the rate your books already apply.
"""


def _currency_export_manifest(company: str, label: str, package: str, files: dict[str, str]) -> str:
    lines = "".join(
        f"  {name:<52} → your {cur} bank account in {package}\n"
        for cur, name in files.items()
    )
    return _EXPORT_MANIFEST_HEADER.format(
        company=company,
        label=label,
        package=package,
        rule="=" * 60,
        files=lines,
    )


def _currency_split_response(
    *,
    transactions: list,
    writer,
    safe_name: str,
    company_name: str,
    label: str,
    package: str,
    suffix: str,
) -> Response:
    """Write one CSV per currency and return it as a file or a ZIP.

    ``writer(rows, path) -> currency`` is the single-currency exporter
    (``export_xero_csv`` / ``export_qbo_csv``); it raises
    ``MixedCurrencyExportError`` if it is ever handed a mixed batch, so the
    split below is enforced by the exporter itself rather than by convention.
    """
    import zipfile

    from core.ledger import group_by_currency

    safe_label = label.replace(" ", "-")
    groups = group_by_currency(transactions)

    written: dict[str, tuple[str, bytes]] = {}
    for currency, rows in groups.items():
        filename = f"{safe_name}_{safe_label}_{suffix}_{currency}.csv"
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            writer(rows, tmp_path)
            written[currency] = (filename, tmp_path.read_bytes())
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    if len(written) == 1:
        currency, (filename, payload) = next(iter(written.items()))
        return Response(
            content=payload,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Export-Currencies": currency,
            },
        )

    zip_name = f"{safe_name}_{safe_label}_{suffix}_by-currency.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for _currency, (filename, payload) in written.items():
            zf.writestr(filename, payload)
        zf.writestr(
            "README.txt",
            _currency_export_manifest(
                company_name, label, package,
                {cur: name for cur, (name, _b) in written.items()},
            ),
        )
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "X-Export-Currencies": ",".join(written),
        },
    )


@router.get("/years")
async def list_years(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List all years that have transaction data.

    Returns year-level summaries: total transactions, income, expenses,
    resolution rate (has match OR link). Powers the Year Index page.
    """
    excl, xparams = transfer_exclusion_sql(alias="t")
    countable, cparams = countable_sql(alias="t")
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Per-year per-currency money figures — currencies are never
            # numerically summed; the scalar income/expenses keys remain
            # (as cross-currency sums) for backward compatibility only.
            cur.execute(
                f"""SELECT EXTRACT(YEAR FROM t.date_posted::date) AS yr,
                           t.currency,
                           SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                     AND NOT {excl} THEN ABS(t.amount::numeric) ELSE 0 END) AS income,
                           SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                     AND NOT {excl} THEN ABS(t.amount::numeric) ELSE 0 END) AS expenses
                    FROM transactions t
                    WHERE t.user_id = %s
                      AND {countable}
                    GROUP BY yr, t.currency""",
                [*xparams, *xparams, db.user_id, *cparams],
            )
            by_currency_by_year: dict[int, list[dict]] = {}
            for r in cur.fetchall():
                income = float(r[2] or 0)
                expenses = float(r[3] or 0)
                by_currency_by_year.setdefault(int(r[0]), []).append({
                    "currency": r[1] or "CAD",
                    "income": round(income, 2),
                    "expenses": round(expenses, 2),
                    "net": round(income - expenses, 2),
                })
            for lst in by_currency_by_year.values():
                lst.sort(key=lambda c: (
                    c["currency"] != "CAD",
                    c["currency"] != "USD",
                    -(abs(c["income"]) + abs(c["expenses"])),
                    c["currency"],
                ))

            cur.execute(
                f"""SELECT EXTRACT(YEAR FROM t.date_posted::date) as yr,
                          COUNT(*) as txn_count,
                          SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                    AND NOT {excl} THEN ABS(t.amount::numeric) ELSE 0 END) as income,
                          -- ABS per row: stored DEBIT signs vary by ingest
                          -- path (legacy CSV negative, LLM parser positive);
                          -- summing raw values nets them out.
                          SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                    AND NOT {excl} THEN ABS(t.amount::numeric) ELSE 0 END) as expenses,
                          COUNT(DISTINCT EXTRACT(MONTH FROM t.date_posted::date)) as months_with_data,
                          COUNT(DISTINCT t.account) as total_accounts
                   FROM transactions t
                   WHERE t.user_id = %s
                     AND {countable}
                   GROUP BY yr
                   ORDER BY yr DESC""",
                [*xparams, *xparams, db.user_id, *cparams],
            )

            years = []
            for row in cur.fetchall():
                yr = int(row[0])
                txn_count = row[1]
                income = float(row[2] or 0)
                expenses = abs(float(row[3] or 0))
                months_with_data = int(row[4])
                total_accounts = int(row[5])

                # Resolved = has match OR link OR status=IGNORED.
                # IGNORED counts as resolved (it needs no receipt), so it lives
                # in both the numerator and denominator. EXCLUDED rows aren't
                # real transactions, so they're out of both.
                cur.execute(
                    f"""SELECT
                           COUNT(*) AS reconcilable,
                           COUNT(*) FILTER (
                               WHERE t.status = 'IGNORED'
                                  OR EXISTS (
                                         SELECT 1 FROM reconciliation_matches m
                                         WHERE m.user_id = t.user_id
                                           AND m.transaction_id = t.id
                                           AND m.status != 'USER_REJECTED'
                                     )
                                  OR EXISTS (
                                         SELECT 1 FROM transaction_links l
                                         WHERE l.user_id = t.user_id
                                           AND l.status != 'USER_REJECTED'
                                           AND (l.source_transaction_id = t.id
                                                OR l.target_transaction_id = t.id)
                                     )
                           ) AS resolved
                       FROM transactions t
                       WHERE t.user_id = %s
                         AND EXTRACT(YEAR FROM t.date_posted::date) = %s
                         AND {countable}""",
                    (db.user_id, yr, *cparams),
                )
                rr = cur.fetchone()
                reconcilable = rr[0] or 0
                resolved = rr[1] or 0
                rate = (resolved / reconcilable * 100) if reconcilable > 0 else 0

                # Count closed months for this year
                cur2_sql = """SELECT COUNT(*) FROM month_status
                              WHERE user_id = %s AND year = %s AND status = 'CLOSED'"""
                cur.execute(cur2_sql, (db.user_id, yr))
                closed_months = cur.fetchone()[0]

                years.append({
                    "year": yr,
                    "total_transactions": txn_count,
                    "total_accounts": total_accounts,
                    "months_with_data": months_with_data,
                    "closed_months": closed_months,
                    "transaction_count": txn_count,
                    "resolved_count": resolved,
                    "reconcilable_count": reconcilable,
                    "income": round(income, 2),
                    "expenses": round(expenses, 2),
                    "net": round(income - expenses, 2),
                    "by_currency": by_currency_by_year.get(yr, []),
                    "resolution_rate": round(rate, 1),
                })

            return {"years": years}


@router.get("/shared-links")
async def list_shared_links(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List all shared report links for the current user.

    Returns token, year, month, created_at, expires_at, and a computed
    is_expired flag.  Ordered by created_at DESC (newest first).
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT token, year, month, created_at, expires_at
                   FROM shared_reports
                   WHERE user_id = %s
                   ORDER BY created_at DESC""",
                (db.user_id,),
            )
            rows = cur.fetchall()

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    links = []
    for row in rows:
        expires_at = row[4]
        # Ensure timezone-aware comparison
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        links.append({
            "token": row[0],
            "year": row[1],
            "month": row[2],
            "created_at": row[3].isoformat(),
            "expires_at": expires_at.isoformat(),
            "is_expired": now > expires_at,
        })

    return {"links": links}


@router.get("/{year}")
async def year_report(
    year: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return month-by-month breakdown for a year.

    Powers the Month View page (account cards per month).
    """
    months = db.get_monthly_status(year)
    return {"year": year, "months": months}


@router.get("/{year}/tax-summary")
async def tax_summary(
    year: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Sales-tax summary (GST/HST/PST/other) aggregated from extracted receipts.

    Sums per month grouped by currency, with quarterly subtotals and a year
    total. Receipts without a parseable date are returned under ``undated``.
    Must stay defined above ``/{year}/{month}`` — Starlette matches that
    pattern against literal segments like ``tax-summary`` and would 422.
    """
    return db.get_tax_summary(year)


@router.get("/{year}/binder-full-year")
async def download_full_year_binder(
    year: int,
    user: AuthUser = Depends(get_current_user_header_or_query),
    db: DatabasePg = Depends(get_db_header_or_query),
):
    """Generate and return a full-year ZIP record binder with monthly subfolders.

    Each month with data gets its own subfolder containing index.html,
    XLSX ledger, proof_of_transaction/, and README.txt. Months with no data are omitted.
    """
    import io
    import zipfile

    try:
        from core.export_binder import generate_audit_binder
    except ImportError as e:
        from server.errors import api_error
        raise api_error(501, "AUDIT_BINDER_UNAVAILABLE",
                        "Record binder generation is not available on this server.",
                        log_exc=e)

    profile = db.get_business_profile()
    company_name = profile.get("company_name", "Company") if profile else "Company"

    # Find months with data
    months_with_data = []
    for month in range(1, 13):
        txns = db.get_transactions_by_month(year, month)
        if txns:
            months_with_data.append(month)

    if not months_with_data:
        raise HTTPException(status_code=404, detail=f"No transactions recorded for {year}")

    all_links = db.get_all_transaction_links()
    transfer_ids = db.get_transfer_linked_txn_ids()

    # Generate per-month binders and nest in subfolders
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Year-level README
        zf.writestr("README.txt", _build_year_readme(company_name, year, months_with_data))

        for month in months_with_data:
            txns = db.get_transactions_by_month(year, month)
            doc_paths, vendors = db.get_matched_doc_paths_and_vendors()
            label = _month_label(year, month)
            folder = f"{year:04d}{month:02d}_{MONTH_ABBREVS[month]}"

            month_txn_ids = {t.id for t in txns if t.id is not None}
            month_links = [
                link for link in all_links
                if link.get("source_transaction_id") in month_txn_ids
                   and link.get("target_transaction_id") in month_txn_ids
            ]

            # Resolve the storage path for each unique statement source_file
            # in this month so the binder can bundle them under statements/
            # and the HTML report can link to each one per account.
            statement_paths: dict[str, str] = {}
            for sf in {t.source_file for t in txns if getattr(t, "source_file", None)}:
                try:
                    sp = db.get_processed_file_stored_path(sf)
                except Exception:
                    sp = None
                if sp:
                    statement_paths[sf] = sp

            month_zip_bytes = await generate_audit_binder(
                company_name, year, month, label, txns, doc_paths, vendors, user.id,
                transaction_links=month_links,
                statement_paths=statement_paths,
                transfer_txn_ids=transfer_ids,
            )

            # Extract month ZIP into subfolder
            with zipfile.ZipFile(io.BytesIO(month_zip_bytes), "r") as mz:
                for fi in mz.infolist():
                    zf.writestr(f"{folder}/{fi.filename}", mz.read(fi.filename))

    zip_bytes = zip_buf.getvalue()
    zip_buf.close()

    safe_name = _sanitize_filename(company_name)
    filename = f"{safe_name}_{year}_FullYear_AuditBinder.zip"

    # Upload to the file store for persistence (fire-and-forget)
    try:
        _try_storage_upload(user.id, zip_bytes, filename, "application/zip")
    except Exception:
        pass

    # Audit log
    try:
        db.insert_audit_log(
            entity_type="export",
            entity_id=0,
            action="EXPORT_ZIP",
            performed_by=user.id,
            new_value={"year": year, "filename": filename, "scope": "full_year"},
        )
    except Exception:
        logger.warning("Failed to write audit log for full-year binder export %s", year)

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_year_readme(company_name: str, year: int, months_with_data: list[int]) -> str:
    """Build README.txt for the full-year record binder."""
    import calendar
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    month_list = "\n".join(
        f"  {year:04d}{m:02d}_{MONTH_ABBREVS[m]}/  — {calendar.month_name[m]} {year}"
        for m in months_with_data
    )
    return f"""{company_name} — Full Year Record Binder
{'=' * 55}

Year:           {year}
Generated:      {now}
Months Included: {len(months_with_data)}

Contents
--------
Each monthly subfolder contains:
  index.html                — Interactive HTML report (open in browser)
  *.xlsx                    — Transaction ledger
  proof_of_transaction/     — Matched proofs of transaction (absent when the
                              month archived none — each month's README.txt
                              gives the exact count)
  README.txt                — Monthly details, including how every transaction
                              in that month is supported

Monthly Folders
---------------
{month_list}

IMPORTANT: Extract the entire ZIP before opening any index.html file.

Retention
---------
Canadian business records are generally kept for six years from the end of the
last tax year they relate to. This is record-keeping guidance, not a
certification — verify the period that applies to you with your accountant.

This ZIP is not encrypted, so any reviewer can open it without a password.

Generated by Autonomous Accounting
{os.environ.get('PUBLIC_SITE_URL', '')}
"""


@router.get("/{year}/{month}")
async def month_report(
    year: int,
    month: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return detailed month report: transactions grouped by account.

    Powers the Account Detail page (transaction table with receipt links).
    """
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be 1-12")

    txns = db.get_transactions_by_month(year, month)
    doc_paths, vendors = db.get_matched_doc_paths_and_vendors()
    doc_paths = _flatten_first(doc_paths)
    vendors = _flatten_first(vendors)

    # Also fetch document IDs and match explanations for building download URLs
    doc_ids: dict[int, int] = {}
    match_explanations: dict[int, str] = {}
    match_scores: dict[int, float] = {}
    match_sub_scores: dict[int, dict] = {}
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT rm.transaction_id, rm.document_id,
                          rm.explanation, rm.confidence_score,
                          rm.amount_score, rm.date_score, rm.vendor_score
                   FROM reconciliation_matches rm
                   WHERE rm.user_id = %s
                     AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED', 'PENDING_REVIEW')""",
                (db.user_id,),
            )
            for row in cur.fetchall():
                doc_ids[row[0]] = row[1]
                if row[2]:
                    match_explanations[row[0]] = row[2]
                if row[3] is not None:
                    match_scores[row[0]] = float(row[3])
                if row[4] is not None or row[5] is not None or row[6] is not None:
                    match_sub_scores[row[0]] = {
                        "amount_score": float(row[4]) if row[4] is not None else None,
                        "date_score": float(row[5]) if row[5] is not None else None,
                        "vendor_score": float(row[6]) if row[6] is not None else None,
                    }

    # Build lookup of this month's transactions to inline the linked
    # counterpart whenever both sides are in the same batch.
    txn_lookup: dict[int, object] = {t.id: t for t in txns if t.id is not None}
    counterpart_index = build_counterpart_index(
        db.get_all_transaction_links(), txn_lookup
    )

    def _linked_summaries(txn_id: int) -> list[dict]:
        return linked_summaries(txn_id, counterpart_index, txn_lookup)

    # Group by account. Sort by (date, id) first — DB insertion order is not
    # chronological, and the UI table renders rows exactly as returned.
    by_account: dict[str, list] = {}
    for txn in sorted(txns, key=lambda t: (t.date_posted, t.id or 0)):
        account = account_str(txn.account)
        has_receipt = txn.id in doc_paths
        receipt_doc_id = doc_ids.get(txn.id)
        by_account.setdefault(account, []).append({
            "id": txn.id,
            "date": txn.date_posted.isoformat(),
            "description": txn.description,
            "amount": str(txn.amount),
            "currency": txn.currency,
            "type": txn.transaction_type,
            "status": txn.status.value,
            "has_receipt": has_receipt,
            "receipt_path": doc_paths.get(txn.id),
            "receipt_doc_id": receipt_doc_id,
            "vendor": vendors.get(txn.id),
            "category": txn.category,
            # `linked_to` stays the first counterpart for callers written
            # against the 1:1 shape; `linked_to_all` is the whole group.
            "linked_to": (_linked_summaries(txn.id) or [None])[0],
            "linked_to_all": _linked_summaries(txn.id),
            "match_explanation": match_explanations.get(txn.id),
            "match_score": match_scores.get(txn.id),
            "match_sub_scores": match_sub_scores.get(txn.id),
        })

    # Account summaries. Internal transfers (linked USD<->CAD moves, credit
    # card payments, transfer-like categories) stay listed in the table but
    # are excluded from income/expense totals to avoid double-counting.
    # Totals are per currency — an account (e.g. Wise) can hold several
    # currencies and those must never be numerically summed. The scalar
    # income/expenses keys remain (cross-currency sums) for compatibility.
    transfer_ids = db.get_transfer_linked_txn_ids()

    # Stable account-tab order: known accounts first, the rest alphabetical —
    # matches the ordering used by the PDF / audit-binder exports.
    _preferred = account_order()
    ordered_names = [k for k in _preferred if k in by_account] + sorted(
        k for k in by_account if k not in _preferred
    )

    accounts = []
    for account_name in ordered_names:
        account_txns = by_account[account_name]
        cur_totals: dict[str, dict] = {}
        for t in account_txns:
            if t["id"] in transfer_ids or is_transfer_category(t.get("category")):
                continue
            cur = t.get("currency") or "CAD"
            bucket = cur_totals.setdefault(cur, {"income": 0.0, "expenses": 0.0})
            if t["type"] == "CREDIT":
                bucket["income"] += abs(float(t["amount"]))
            else:
                bucket["expenses"] += abs(float(t["amount"]))
        currency_totals = [
            {
                "currency": cur,
                "income": round(v["income"], 2),
                "expenses": round(v["expenses"], 2),
                "net": round(v["income"] - v["expenses"], 2),
            }
            for cur, v in cur_totals.items()
        ]
        currency_totals.sort(key=lambda c: (
            c["currency"] != "CAD",
            c["currency"] != "USD",
            -(abs(c["income"]) + abs(c["expenses"])),
            c["currency"],
        ))
        accounts.append({
            "account": account_name,
            "transaction_count": len(account_txns),
            "income": round(sum(c["income"] for c in currency_totals), 2),
            "expenses": round(sum(c["expenses"] for c in currency_totals), 2),
            "currency_totals": currency_totals,
            "transactions": account_txns,
        })

    label = _month_label(year, month)

    # Include close status
    close_status = db.get_month_statuses(year).get(month, {"status": "OPEN"})

    # Company name for the report header
    profile = db.get_business_profile()
    company_name = profile.get("company_name") if profile else None

    return {
        "year": year,
        "month": month,
        "label": label,
        "total_transactions": len(txns),
        "company_name": company_name,
        "accounts": accounts,
        "closed": close_status.get("status") == "CLOSED",
        "closed_at": close_status.get("closed_at"),
    }


@router.post("/{year}/{month}/share")
async def create_share_link(
    year: int,
    month: int,
    hours: int = Query(72, ge=1, le=720),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Create a time-limited shareable link for a month report.

    The shared link is public — accountants can view it without an account.
    Default expiry: 72 hours (3 days). Max: 720 hours (30 days).
    """
    import secrets
    from datetime import datetime, timedelta, timezone

    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be 1-12")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)

    share_id = db.create_shared_report(year, month, token, expires_at)

    # Audit log
    try:
        db.insert_audit_log(
            entity_type="report",
            entity_id=share_id,
            action="SHARE_LINK_CREATE",
            performed_by=user.id,
            new_value={
                "year": year,
                "month": month,
                "expires_at": expires_at.isoformat(),
                "hours": hours,
            },
        )
    except Exception:
        logger.warning("Failed to write audit log for share link creation %s/%s", year, month)

    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "url": f"/shared/{token}",
    }


@router.delete("/shared/{token}")
async def revoke_share_link(
    token: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Revoke a shared report link."""
    share_info = None
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Fetch share link details before deleting (for audit log)
            cur.execute(
                "SELECT id, year, month FROM shared_reports WHERE token = %s AND user_id = %s",
                (token, db.user_id),
            )
            row = cur.fetchone()
            if row:
                share_info = {"id": row[0], "year": row[1], "month": row[2]}

            cur.execute(
                "DELETE FROM shared_reports WHERE token = %s AND user_id = %s",
                (token, db.user_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Share link not found")

    # Audit log
    if share_info:
        try:
            db.insert_audit_log(
                entity_type="report",
                entity_id=share_info["id"],
                action="SHARE_LINK_REVOKE",
                performed_by=user.id,
                old_value={
                    "year": share_info["year"],
                    "month": share_info["month"],
                },
            )
        except Exception:
            logger.warning("Failed to write audit log for share link revocation")

    return {"status": "ok"}


@router.get("/{year}/{month}/pdf")
async def download_pdf(
    year: int,
    month: int,
    user: AuthUser = Depends(get_current_user_header_or_query),
    db: DatabasePg = Depends(get_db_header_or_query),
):
    """Generate and return a professional PDF report for a month.

    Uses ReportLab to generate a PDF with:
    - Company header
    - Transaction tables grouped by account
    - Proof of transaction references (file-store URLs)
    - Summary totals

    Attempts to upload to the file store and return a signed URL. Falls back to
    direct binary response if storage upload fails.
    """
    try:
        from core.report_pdf import generate_month_pdf
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="PDF generation not available — install reportlab",
        )

    profile = db.get_business_profile()
    company_name = profile.get("company_name", "Company") if profile else "Company"

    txns = db.get_transactions_by_month(year, month)
    doc_paths, vendors = db.get_matched_doc_paths_and_vendors()

    if not txns:
        raise HTTPException(status_code=404, detail=f"No transactions for {year}-{month:02d}")

    label = _month_label(year, month)
    pdf_bytes = generate_month_pdf(
        company_name=company_name,
        year=year,
        month=month,
        label=label,
        transactions=txns,
        doc_paths=doc_paths,
        vendors=vendors,
        transfer_txn_ids=db.get_transfer_linked_txn_ids(),
    )

    safe_name = _sanitize_filename(company_name)
    filename = f"{safe_name}_{label}_Report.pdf"

    # Upload to the file store for persistence (fire-and-forget)
    try:
        _try_storage_upload(user.id, pdf_bytes, filename, "application/pdf")
    except Exception:
        pass

    # Audit log
    try:
        db.insert_audit_log(
            entity_type="export",
            entity_id=0,
            action="EXPORT_PDF",
            performed_by=user.id,
            new_value={"year": year, "month": month, "filename": filename},
        )
    except Exception:
        logger.warning("Failed to write audit log for PDF export %s/%s", year, month)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/{year}/{month}/binder")
async def download_binder(
    year: int,
    month: int,
    user: AuthUser = Depends(get_current_user_header_or_query),
    db: DatabasePg = Depends(get_db_header_or_query),
):
    """Generate and return a ZIP record binder for the month.

    Contains:
    - XLSX ledger
    - An organized proof_of_transaction folder with every matched document
    - No encryption, so any reviewer can open it without a password

    Attempts a file-store upload with signed-URL fallback to direct download.
    """
    try:
        from core.export_binder import generate_audit_binder
    except ImportError as e:
        from server.errors import api_error
        raise api_error(501, "AUDIT_BINDER_UNAVAILABLE",
                        "Record binder generation is not available on this server.",
                        log_exc=e)

    profile = db.get_business_profile()
    company_name = profile.get("company_name", "Company") if profile else "Company"

    txns = db.get_transactions_by_month(year, month)
    doc_paths, vendors = db.get_matched_doc_paths_and_vendors()

    if not txns:
        raise HTTPException(status_code=404, detail=f"No transactions for {year}-{month:02d}")

    # Collect transaction_links for in-month navigation + Transfers Summary
    month_txn_ids = {t.id for t in txns if t.id is not None}
    all_links = db.get_all_transaction_links()
    month_links = [
        link for link in all_links
        if link.get("source_transaction_id") in month_txn_ids
           and link.get("target_transaction_id") in month_txn_ids
    ]

    label = _month_label(year, month)

    # Resolve the storage path for each unique statement source_file in
    # this month so the binder can bundle them under statements/ and the
    # HTML report can link to each one per account section.
    statement_paths: dict[str, str] = {}
    for sf in {t.source_file for t in txns if getattr(t, "source_file", None)}:
        try:
            sp = db.get_processed_file_stored_path(sf)
        except Exception:
            sp = None
        if sp:
            statement_paths[sf] = sp

    zip_bytes = await generate_audit_binder(
        company_name=company_name,
        year=year,
        month=month,
        label=label,
        transactions=txns,
        doc_paths=doc_paths,
        vendors=vendors,
        user_id=user.id,
        transaction_links=month_links,
        statement_paths=statement_paths,
        transfer_txn_ids=db.get_transfer_linked_txn_ids(),
    )

    safe_name = _sanitize_filename(company_name)
    safe_label = label.replace(" ", "_")
    filename = f"{safe_name}_{safe_label}_AuditBinder.zip"

    # Upload to the file store for persistence (fire-and-forget)
    try:
        _try_storage_upload(user.id, zip_bytes, filename, "application/zip")
    except Exception:
        pass

    # Audit log
    try:
        db.insert_audit_log(
            entity_type="export",
            entity_id=0,
            action="EXPORT_ZIP",
            performed_by=user.id,
            new_value={"year": year, "month": month, "filename": filename},
        )
    except Exception:
        logger.warning("Failed to write audit log for binder export %s/%s", year, month)

    # Always return binary directly — signed URL redirects produce UUID
    # filenames in Chrome because the download attribute is ignored for
    # cross-origin hrefs.
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/{year}/{month}/close")
async def close_month(
    year: int,
    month: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Close a month for reporting."""
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be 1-12")

    # Check if already closed
    if db.is_month_closed(year, month):
        label = _month_label(year, month)
        raise HTTPException(status_code=409, detail=f"Month {label} is already closed")

    # Check if month has transactions
    txns = db.get_transactions_by_month(year, month)
    if not txns:
        raise HTTPException(status_code=422, detail="Cannot close month with 0 transactions")

    db.close_month(year, month, closed_by=user.id)

    # Compute resolved count from the authoritative join tables so the
    # banner reflects the same semantics as the dashboard KPIs. IGNORED
    # transactions count as resolved (user has dismissed them).
    txn_ids = [t.id for t in txns]
    reconcilable = len(txn_ids)
    resolved = 0
    if txn_ids:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(DISTINCT t.id)
                       FROM transactions t
                       WHERE t.user_id = %s
                         AND t.id = ANY(%s)
                         AND (
                             t.status = 'IGNORED'
                             OR EXISTS (
                                 SELECT 1 FROM reconciliation_matches m
                                 WHERE m.user_id = t.user_id
                                   AND m.transaction_id = t.id
                                   AND m.status != 'USER_REJECTED'
                             )
                             OR EXISTS (
                                 SELECT 1 FROM transaction_links l
                                 WHERE l.user_id = t.user_id
                                   AND l.status != 'USER_REJECTED'
                                   AND (l.source_transaction_id = t.id
                                        OR l.target_transaction_id = t.id)
                             )
                         )""",
                    (db.user_id, txn_ids),
                )
                resolved = cur.fetchone()[0] or 0

    rate = (resolved / reconcilable * 100) if reconcilable > 0 else 0

    label = _month_label(year, month)
    return {
        "status": "ok",
        "year": year,
        "month": month,
        "label": label,
        "closed": True,
        "closed_at": date.today().isoformat(),
        "transaction_count": len(txns),
        "resolved_count": resolved,
        "unresolved_count": reconcilable - resolved,
        "resolution_rate": round(rate, 1),
    }


@router.post("/{year}/{month}/reopen")
async def reopen_month(
    year: int,
    month: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Reopen a previously closed month."""
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be 1-12")

    if not db.is_month_closed(year, month):
        label = _month_label(year, month)
        raise HTTPException(status_code=409, detail=f"Month {label} is not closed")

    db.reopen_month(year, month)
    label = _month_label(year, month)

    from datetime import datetime, timezone
    return {
        "status": "ok",
        "year": year,
        "month": month,
        "label": label,
        "closed": False,
        "reopened_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{year}/{month}/export-qbo")
async def export_qbo(
    year: int,
    month: int,
    user: AuthUser = Depends(get_current_user_header_or_query),
    db: DatabasePg = Depends(get_db_header_or_query),
):
    """Export transactions as QuickBooks Online (QBO) compatible CSV.

    Columns: Date (MM/DD/YYYY), Description, Amount, Currency, Account,
    Category, GIFI.

    **One file per currency.** QBO reads a bank-feed CSV's currency from the
    account it is imported into, so a month spanning BMO CAD and BMO USD is
    returned as a ZIP of one CSV per currency (ISO code in each filename) plus
    a README naming the account each file belongs in. Single-currency months
    still return a bare CSV. No amount is ever converted or relabelled.
    """
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be 1-12")

    txns = db.get_transactions_by_month(year, month)
    if not txns:
        raise HTTPException(status_code=404, detail=f"No transactions for {year}-{month:02d}")

    doc_paths, vendors = db.get_matched_doc_paths_and_vendors()
    doc_paths = _flatten_first(doc_paths)
    vendors = _flatten_first(vendors)

    # Build categories dict from auto-categorization
    from server.api.reconciliation import _auto_categorize_transactions
    categories = _auto_categorize_transactions(txns)

    try:
        from core.ledger import export_qbo_csv
    except ImportError:
        raise HTTPException(status_code=501, detail="QBO export not available")

    profile = db.get_business_profile()
    company_name = profile.get("company_name", "Company") if profile else "Company"
    label = _month_label(year, month)
    safe_name = _sanitize_filename(company_name)

    response = _currency_split_response(
        transactions=txns,
        writer=lambda rows, path: export_qbo_csv(
            rows, path, matched_docs=doc_paths, categories=categories,
        ),
        safe_name=safe_name,
        company_name=company_name,
        label=label,
        package="QuickBooks Online",
        suffix="QBO",
    )

    filename = re.sub(r'.*filename="|"$', "", response.headers["content-disposition"])

    # Audit log
    try:
        db.insert_audit_log(
            entity_type="export",
            entity_id=0,
            action="EXPORT_CSV",
            performed_by=user.id,
            new_value={
                "year": year, "month": month, "filename": filename, "format": "qbo",
                "currencies": response.headers.get("X-Export-Currencies", ""),
            },
        )
    except Exception:
        logger.warning("Failed to write audit log for QBO export %s/%s", year, month)

    return response


@router.get("/{year}/{month}/export-xero")
async def export_xero(
    year: int,
    month: int,
    user: AuthUser = Depends(get_current_user_header_or_query),
    db: DatabasePg = Depends(get_db_header_or_query),
):
    """Export transactions as Xero compatible CSV.

    Columns: Date (DD/MM/YYYY), Description, Reference, Amount, Currency,
    Account Code, Tax Type, Tax Basis, GIFI.

    **One file per currency** (see :func:`export_qbo`) — Xero takes a bank
    statement's currency from the account it is imported into, so a mixed file
    would book every USD amount as CAD. Multi-currency months come back as a
    ZIP of per-currency CSVs plus a README; nothing is converted.

    ``Tax Type`` is asserted only on evidence: a GST/HST code appears only when
    the linked source document actually shows tax. Uncategorized rows and rows
    with no document get a blank code and a ``Tax Basis`` saying why.
    """
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Month must be 1-12")

    txns = db.get_transactions_by_month(year, month)
    if not txns:
        raise HTTPException(status_code=404, detail=f"No transactions for {year}-{month:02d}")

    doc_paths, vendors = db.get_matched_doc_paths_and_vendors()
    doc_paths = _flatten_first(doc_paths)
    vendors = _flatten_first(vendors)

    from server.api.reconciliation import _auto_categorize_transactions
    categories = _auto_categorize_transactions(txns)

    # GST/HST evidence: the only thing that can justify an input tax credit.
    try:
        doc_tax = _doc_tax_by_txn(db)
    except Exception:
        logger.warning("Could not read document tax for Xero export %s/%s", year, month)
        doc_tax = {}

    try:
        from core.ledger import export_xero_csv
    except ImportError:
        raise HTTPException(status_code=501, detail="Xero export not available")

    profile = db.get_business_profile()
    company_name = profile.get("company_name", "Company") if profile else "Company"
    label = _month_label(year, month)
    safe_name = _sanitize_filename(company_name)

    response = _currency_split_response(
        transactions=txns,
        writer=lambda rows, path: export_xero_csv(
            rows, path, matched_docs=doc_paths, categories=categories,
            doc_tax=doc_tax, vendors=vendors,
        ),
        safe_name=safe_name,
        company_name=company_name,
        label=label,
        package="Xero",
        suffix="Xero",
    )

    filename = re.sub(r'.*filename="|"$', "", response.headers["content-disposition"])

    # Audit log
    try:
        db.insert_audit_log(
            entity_type="export",
            entity_id=0,
            action="EXPORT_CSV",
            performed_by=user.id,
            new_value={
                "year": year, "month": month, "filename": filename, "format": "xero",
                "currencies": response.headers.get("X-Export-Currencies", ""),
            },
        )
    except Exception:
        logger.warning("Failed to write audit log for Xero export %s/%s", year, month)

    return response


def _try_storage_upload(
    user_id: str, file_bytes: bytes, filename: str, content_type: str
) -> str | None:
    """Attempt to upload an export to the file store and return a signed download URL.

    Returns None if storage is not configured or upload fails.
    """
    try:
        from core.file_storage import generate_presigned_url, upload_export
        storage_path = upload_export(user_id, file_bytes, filename, content_type)
        url = generate_presigned_url(storage_path, expires_in=3600)
        return url
    except ImportError:
        return None
    except Exception:
        logger.warning("Storage upload failed for export %s, falling back to direct download", filename)
        return None
