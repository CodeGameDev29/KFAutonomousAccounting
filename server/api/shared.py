"""Shared report API — public, no authentication required.

Accountants access shared reports via time-limited token links.
No account creation needed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from config.data_files import account_order
from config.settings import MONTH_ABBREVS
from core.transfer_exclusion import countable_sql
from db.connection import get_pool
from db.database_pg import DatabasePg
from models.transaction import account_str
from server.api.link_view import build_counterpart_index, linked_summaries

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/shared", tags=["shared"])

# Per-route rate limiter for public shared report endpoints
limiter = Limiter(key_func=get_remote_address)


def _month_label(year: int, month: int) -> str:
    return f"{MONTH_ABBREVS[month]} {year}"


def _flatten_first(d: dict[int, list[str]]) -> dict[int, str]:
    """Take first element from multi-proof lists for backward compat."""
    return {k: v[0] for k, v in d.items() if v}


_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{20,64}$")


@router.get("/{token}")
@limiter.limit("30/minute")
async def get_shared_report(token: str, request: Request):
    """Retrieve a shared report by its token.

    Public endpoint — no authentication required.
    Returns the full month report if the token is valid and not expired.
    """
    # Validate token format to reject most guesses early
    if not _TOKEN_PATTERN.match(token):
        raise HTTPException(status_code=404, detail="Report not found or link expired")

    # Use DatabasePg context manager for all queries to ensure
    # they go through the same user-scoped connection (CWE-863).
    pool = get_pool()

    # Look up the share token — this must use a raw connection, because the
    # user_id is not known yet (it is in the token row).
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id, year, month, expires_at, created_at
                   FROM shared_reports WHERE token = %s""",
                (token,),
            )
            row = cur.fetchone()
    finally:
        pool.putconn(conn)

    if not row:
        raise HTTPException(status_code=404, detail="Report not found or link expired")

    user_id, year, month, expires_at, created_at = row

    # Check expiry
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="This share link has expired")

    # All subsequent queries use the user-scoped DatabasePg context manager
    db = DatabasePg(pool, str(user_id))

    # Get company name
    profile = db.get_business_profile()
    company_name = profile.get("company_name", "Company") if profile else "Company"

    # Get transactions
    txns = db.get_transactions_by_month(year, month)
    doc_paths, vendors = db.get_matched_doc_paths_and_vendors()
    doc_paths = _flatten_first(doc_paths)
    vendors = _flatten_first(vendors)

    # Fetch document IDs + match explanations/scores for matched transactions
    doc_ids: dict[int, int] = {}
    match_info: dict[int, dict] = {}
    with db._conn() as conn_ctx:
        with conn_ctx.cursor() as cur:
            cur.execute(
                """SELECT rm.transaction_id, rm.document_id,
                          rm.confidence_score,
                          rm.amount_score, rm.date_score, rm.vendor_score,
                          rm.explanation
                   FROM reconciliation_matches rm
                   WHERE rm.user_id = %s
                     AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')""",
                (str(user_id),),
            )
            for drow in cur.fetchall():
                doc_ids[drow[0]] = drow[1]
                match_info[drow[0]] = {
                    "match_score": float(drow[2]) if drow[2] is not None else None,
                    "amount_score": float(drow[3]) if drow[3] is not None else None,
                    "date_score": float(drow[4]) if drow[4] is not None else None,
                    "vendor_score": float(drow[5]) if drow[5] is not None else None,
                    "explanation": drow[6],
                }

    # Build lookup of this month's transactions to inline linked
    # counterparts. Accountants viewing a shared report need to see linkage
    # just like the authenticated owner — matched receipts alone are not
    # enough because many transactions (transfers, FX, refunds) rely on
    # link-based proof.
    txn_lookup: dict[int, object] = {t.id: t for t in txns if t.id is not None}
    counterpart_index = build_counterpart_index(
        db.get_all_transaction_links(), txn_lookup
    )

    def _linked_summaries(txn_id: int) -> list[dict]:
        return linked_summaries(txn_id, counterpart_index, txn_lookup)

    # Group by account, sorted by (date, id) — same order as the owner view.
    by_account: dict[str, list] = {}
    for txn in sorted(txns, key=lambda t: (t.date_posted, t.id or 0)):
        account = account_str(txn.account)
        has_receipt = txn.id in doc_paths
        receipt_doc_id = doc_ids.get(txn.id)
        mi = match_info.get(txn.id, {})
        by_account.setdefault(account, []).append({
            "id": txn.id,
            "date": txn.date_posted.isoformat(),
            "description": txn.description,
            "amount": str(txn.amount),
            "currency": txn.currency,
            "type": txn.transaction_type,
            "status": txn.status.value,
            "has_receipt": has_receipt,
            "receipt_doc_id": receipt_doc_id,
            "vendor": vendors.get(txn.id),
            "category": txn.category,
            # `linked_to` stays the first counterpart for callers written
            # against the 1:1 shape; `linked_to_all` is the whole group.
            "linked_to": (_linked_summaries(txn.id) or [None])[0],
            "linked_to_all": _linked_summaries(txn.id),
            "match_score": mi.get("match_score"),
            "match_explanation": mi.get("explanation"),
            "match_sub_scores": {
                "amount_score": mi.get("amount_score"),
                "date_score": mi.get("date_score"),
                "vendor_score": mi.get("vendor_score"),
            } if mi else None,
        })

    # Internal transfers stay listed but are excluded from income/expense
    # totals — same rule as the owner's report view (no double-counting).
    # Totals are per currency (one multi-currency account can hold USD, EUR and
    # CAD balances at once) and income uses abs() per row, matching the month
    # report exactly — stored signs vary by parser, and the accountant view and
    # the owner view must never disagree.
    transfer_ids = db.get_transfer_linked_txn_ids()

    # Stable account-tab order: known accounts first, rest alphabetical —
    # identical to the owner month report.
    _preferred = account_order()
    ordered_names = [k for k in _preferred if k in by_account] + sorted(
        k for k in by_account if k not in _preferred
    )

    accounts = []
    for account_name in ordered_names:
        account_txns = by_account[account_name]
        cur_totals: dict[str, dict] = {}
        for t in account_txns:
            if t["id"] in transfer_ids:
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

    # Compute resolution_rate for the record-completeness badge. IGNORED counts
    # as resolved (needs no receipt); EXCLUDED rows are not real transactions
    # and are out of both numerator and denominator.
    countable, cparams = countable_sql(alias="t")
    with db._conn() as conn_ctx:
        with conn_ctx.cursor() as cur:
            cur.execute(
                f"""SELECT
                       COUNT(*) AS total,
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
                     AND EXTRACT(MONTH FROM t.date_posted::date) = %s
                     AND {countable}""",
                (str(user_id), year, month, *cparams),
            )
            rr_row = cur.fetchone()
            total_for_rate = rr_row[0] or 0
            resolved_for_rate = rr_row[1] or 0
            resolution_rate = (resolved_for_rate / total_for_rate * 100) if total_for_rate > 0 else 0

    label = _month_label(year, month)
    return {
        "company_name": company_name,
        "year": year,
        "month": month,
        "label": label,
        "total_transactions": len(txns),
        "resolution_rate": round(resolution_rate, 1),
        "resolved_count": resolved_for_rate,
        "accounts": accounts,
        "expires_at": expires_at.isoformat(),
        "generated_at": created_at.isoformat() if created_at else None,
        "shared": True,
    }


@router.get("/{token}/receipt/{doc_id}")
@limiter.limit("30/minute")
async def get_shared_receipt(request: Request, token: str, doc_id: int):
    """Get a presigned download URL for a receipt in a shared report.

    Public endpoint — validates token before allowing access.
    """
    pool = get_pool()

    # Look up token using raw connection (no user_id yet)
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id, year, month, expires_at
                   FROM shared_reports WHERE token = %s""",
                (token,),
            )
            row = cur.fetchone()
    finally:
        pool.putconn(conn)

    if not row:
        raise HTTPException(status_code=404, detail="Report not found")

    user_id, year, month, expires_at = row

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="This share link has expired")

    # Use user-scoped DatabasePg for all document queries
    db = DatabasePg(pool, str(user_id))

    # Get document stored_path — verify doc belongs to a transaction
    # in the shared month to prevent IDOR across months (CWE-639)
    with db._conn() as conn_ctx:
        with conn_ctx.cursor() as cur:
            cur.execute(
                """SELECT d.stored_path, d.original_filename
                   FROM documents d
                   JOIN reconciliation_matches rm ON rm.document_id = d.id
                   JOIN transactions t ON rm.transaction_id = t.id
                   WHERE d.id = %s AND d.user_id = %s
                     AND EXTRACT(YEAR FROM t.date_posted::date) = %s
                     AND EXTRACT(MONTH FROM t.date_posted::date) = %s
                     AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED')
                   LIMIT 1""",
                (doc_id, str(user_id), year, month),
            )
            doc_row = cur.fetchone()

    if not doc_row:
        raise HTTPException(status_code=404, detail="Proof of transaction not found")

    stored_path = doc_row[0]
    filename = doc_row[1]

    if not stored_path:
        raise HTTPException(status_code=404, detail="Proof of transaction file not available")

    # Generate signed download URL
    try:
        from core.file_storage import get_download_url
        url = get_download_url(stored_path, filename)
        return {"url": url, "filename": filename}
    except Exception as e:
        logger.error("Failed to generate download URL for shared receipt: %s", e)
        raise HTTPException(status_code=500, detail="Could not generate download URL")
