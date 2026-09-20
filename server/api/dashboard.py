"""Dashboard API — summary cards, monthly status, recent transactions, cost tracking."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query

from core.transfer_exclusion import countable_sql, transfer_exclusion_sql
from db.database_pg import DatabasePg
from models.transaction import account_str
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _financials_for_prefix(db: DatabasePg, prefix: str) -> dict:
    """Income/expenses for a date-prefix, excluding internal transfers.

    Returns per-currency figures in ``by_currency`` (CAD first, then USD,
    then by activity) — amounts in different currencies must never be
    numerically summed. The legacy scalar ``income``/``expenses``/``net``
    keys are retained for backward compatibility but are cross-currency
    sums; new UI reads ``by_currency`` only.
    """
    excl, xparams = transfer_exclusion_sql(alias="t")
    countable, cparams = countable_sql(alias="t")
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT
                       t.currency,
                       COALESCE(SUM(CASE WHEN t.transaction_type = 'CREDIT'
                                       THEN ABS(CAST(t.amount AS NUMERIC)) ELSE 0 END), 0) AS income,
                       COALESCE(SUM(CASE WHEN t.transaction_type = 'DEBIT'
                                       THEN ABS(CAST(t.amount AS NUMERIC)) ELSE 0 END), 0) AS expenses
                   FROM transactions t
                   WHERE t.user_id = %s AND t.date_posted LIKE %s
                     AND NOT {excl}
                     AND {countable}
                   GROUP BY t.currency""",
                [db.user_id, prefix, *xparams, *cparams],
            )
            rows = cur.fetchall()

    by_currency = []
    for r in rows:
        income = Decimal(str(r[1]))
        expenses = Decimal(str(r[2]))
        by_currency.append({
            "currency": r[0] or "CAD",
            "income": round(float(income), 2),
            "expenses": round(float(expenses), 2),
            "net": round(float(income - expenses), 2),
        })
    by_currency.sort(key=lambda c: (
        c["currency"] != "CAD",
        c["currency"] != "USD",
        -(abs(c["income"]) + abs(c["expenses"])),
        c["currency"],
    ))

    total_income = sum(c["income"] for c in by_currency)
    total_expenses = sum(c["expenses"] for c in by_currency)
    return {
        "income": round(total_income, 2),
        "expenses": round(total_expenses, 2),
        "net": round(total_income - total_expenses, 2),
        "by_currency": by_currency,
    }


def _get_month_financials(db: DatabasePg, year: int, month: int) -> dict:
    """Get income/expenses for a specific month, excluding internal transfers."""
    return _financials_for_prefix(db, f"{year:04d}-{month:02d}-%")


def _get_ytd_financials(db: DatabasePg, year: int) -> dict:
    """Get year-to-date income/expenses, excluding internal transfers."""
    return _financials_for_prefix(db, f"{year:04d}-%")


@router.get("/summary")
async def dashboard_summary(
    month: int | None = Query(None, ge=1, le=12),
    year: int | None = Query(None, ge=2000, le=2100),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return dashboard summary: transaction counts, match rate, income/expense totals.

    Powers the 4 summary cards at the top of the Dashboard page.
    Accepts optional month/year params for period-specific data.
    """
    summary = db.get_dashboard_summary()

    # Determine which month to show
    if month is not None and year is not None:
        # Explicit month/year requested
        target_month = month
        target_year = year
    else:
        # Auto-detect from the most recent transaction month
        latest = db.get_latest_transaction_month()
        if latest:
            target_year, target_month = latest
        else:
            now = date.today()
            target_year, target_month = now.year, now.month

    # Get financials for the target month
    current = _get_month_financials(db, target_year, target_month)
    summary["income_this_month"] = current["income"]
    summary["expenses_this_month"] = current["expenses"]
    summary["net_this_month"] = current["net"]
    summary["financials_this_month_by_currency"] = current["by_currency"]

    # Get financials for the previous month (for trend arrows)
    if target_month == 1:
        prev_year, prev_month = target_year - 1, 12
    else:
        prev_year, prev_month = target_year, target_month - 1

    prev = _get_month_financials(db, prev_year, prev_month)
    summary["income_prev_month"] = prev["income"]
    summary["expenses_prev_month"] = prev["expenses"]
    summary["net_prev_month"] = prev["net"]
    summary["financials_prev_month_by_currency"] = prev["by_currency"]

    # Add month metadata
    summary["month"] = target_month
    summary["year"] = target_year
    summary["month_label"] = f"{MONTH_NAMES[target_month - 1]} {target_year}"

    # Year-to-date financials
    ytd = _get_ytd_financials(db, target_year)
    summary["income_ytd"] = ytd["income"]
    summary["expenses_ytd"] = ytd["expenses"]
    summary["net_ytd"] = ytd["net"]
    summary["financials_ytd_by_currency"] = ytd["by_currency"]

    return summary


@router.get("/monthly-status")
async def monthly_status(
    year: int | None = None,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return per-month status for the fiscal year.

    Each month includes: transaction count, matched count, missing receipt count,
    total income, total expenses, and completeness status.
    """
    if year is None:
        # Auto-detect year: use the year that has the most recent transaction data
        latest = db.get_latest_transaction_month()
        if latest:
            year = latest[0]
        else:
            year = date.today().year
    months = db.get_monthly_status(year)
    return {"year": year, "months": months}


@router.get("/recent-transactions")
async def recent_transactions(
    limit: int = 10,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return the most recent transactions for the activity feed."""
    limit = min(limit, 50)
    txns = db.get_recent_transactions(limit)

    # Enrich with match status and document info
    doc_paths = db.get_matched_document_paths()

    result = []
    for txn in txns:
        has_receipt = txn.id in doc_paths
        result.append({
            "id": txn.id,
            "date": txn.date_posted.isoformat(),
            "description": txn.description,
            "amount": str(txn.amount),
            "currency": txn.currency,
            "account": account_str(txn.account),
            "type": txn.transaction_type,
            "status": txn.status.value,
            "has_receipt": has_receipt,
            "source_file": txn.source_file,
        })

    return {"transactions": result}


@router.get("/action-items")
async def action_items(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return action items for the banner: pending reviews, missing receipts, etc."""
    items = []

    # Pending reviews
    pending = db.get_pending_reviews()
    if pending:
        items.append({
            "type": "pending_reviews",
            "count": len(pending),
            "message": f"{len(pending)} match{'es' if len(pending) != 1 else ''} need review",
            "action": "/review",
        })

    # Missing receipts (unmatched transactions that aren't non-reconcilable)
    unmatched = db.get_unmatched_transactions()
    if unmatched:
        items.append({
            "type": "missing_receipts",
            "count": len(unmatched),
            "message": f"{len(unmatched)} transaction{'s' if len(unmatched) != 1 else ''} need proofs of transaction",
            "action": "/transactions?filter=unmatched",
        })

    # Onboarding incomplete
    if not db.is_onboarding_complete():
        items.append({
            "type": "onboarding",
            "count": 1,
            "message": "Complete your setup to start syncing",
            "action": "/onboarding",
        })

    # Wise transactions detected but Wise not connected
    from core.credentials_cloud import CloudCredentialStore
    creds = CloudCredentialStore(db, user.id)
    wise_connected = creds.retrieve("wise_api_token") is not None
    if not wise_connected:
        import re
        unmatched_txns = db.get_unmatched_transactions()
        wise_pattern = re.compile(r'WISE\s*PAYMENTS|WISEMSP|WISE\s*MSP|WISE\s*TRANSFER', re.IGNORECASE)
        wise_count = sum(1 for t in unmatched_txns if wise_pattern.search(t.description))
        if wise_count > 0:
            items.append({
                "type": "wise_detected",
                "count": wise_count,
                "message": f"{wise_count} Wise transfer{'s' if wise_count != 1 else ''} detected. Connect Wise for better matching.",
                "action": "/settings?tab=connections",
            })

    return {"items": items}


@router.get("/checklist")
async def dashboard_checklist(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return setup checklist state for the dashboard."""
    state = db.get_checklist_state()

    if state["dismissed"]:
        return {"items": [], "completed_count": 0, "total_count": 0, "all_complete": True, "dismissed": True}

    items = [
        {
            "id": "profile_complete",
            "label": "Set up your business profile",
            "description": "Enter your company name, fiscal year, and base currency",
            "completed": state["profile_complete"],
            "action_url": "/onboarding",
            "priority": 1,
        },
        {
            "id": "bank_connected",
            "label": "Import bank transactions",
            "description": "Upload a bank statement or connect your bank account",
            "completed": state["bank_connected"],
            "action_url": "/transactions",
            "priority": 2,
        },
        {
            "id": "email_connected",
            "label": "Connect your email",
            "description": "Auto-fetch invoices and proofs of transaction from Gmail",
            "completed": state["email_connected"],
            "action_url": "/settings?tab=connections",
            "priority": 3,
        },
        {
            "id": "first_receipt_uploaded",
            "label": "Upload your first proof of transaction",
            "description": "Drag and drop a proof of transaction PDF or image",
            "completed": state["first_receipt_uploaded"],
            "action_url": "/transactions",
            "priority": 4,
        },
        {
            "id": "first_reconciliation_done",
            "label": "Run your first reconciliation",
            "description": "Match transactions to proofs of transaction automatically",
            "completed": state["first_reconciliation_done"],
            "action_url": "/transactions",
            "priority": 5,
        },
    ]

    completed_count = sum(1 for item in items if item["completed"])
    all_complete = completed_count == len(items)

    return {
        "items": items,
        "completed_count": completed_count,
        "total_count": len(items),
        "all_complete": all_complete,
        "dismissed": False,
    }


@router.post("/checklist/dismiss")
async def dismiss_checklist(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Dismiss the setup checklist."""
    db.dismiss_checklist()
    return {"status": "ok", "dismissed": True}


@router.get("/costs")
async def get_costs(
    months: int = 1,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get LLM cost summary for dashboard widget."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30 * months)
    summary = db.get_cost_summary(start.isoformat(), end.isoformat())
    period = end.strftime("%Y-%m")
    return {"period": period, **summary}


@router.get("/setup-completeness")
async def setup_completeness(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return setup completeness for the dashboard progress indicator.

    Checks 5 setup items:
    1. Business profile created
    2. At least one bank statement uploaded (transactions exist)
    3. At least one receipt uploaded (documents exist)
    4. First reconciliation run (any matched transactions)
    5. First export generated (shared_reports or via report download)

    Returns percent (0-100), items list with done/label, and all_complete flag.
    """
    profile = db.get_business_profile()
    has_profile = profile is not None and bool(profile.get("company_name"))

    summary = db.get_dashboard_summary()
    has_transactions = summary.get("transaction_count", 0) > 0
    has_documents = summary.get("document_count", 0) > 0
    has_matches = summary.get("matched_count", 0) > 0

    # Check for any shared report or export
    has_export = False
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM shared_reports WHERE user_id = %s LIMIT 1",
                    (db.user_id,),
                )
                has_export = cur.fetchone() is not None
    except Exception:
        pass

    items = [
        {"key": "profile", "label": "Set up business profile", "done": has_profile},
        {"key": "bank_statement", "label": "Upload a bank statement", "done": has_transactions},
        {"key": "receipt", "label": "Upload a proof of transaction", "done": has_documents},
        {"key": "reconciliation", "label": "Run first reconciliation", "done": has_matches},
        {"key": "export", "label": "Generate a report or export", "done": has_export},
    ]

    done_count = sum(1 for item in items if item["done"])
    total = len(items)
    percent = round(done_count / total * 100) if total > 0 else 0

    return {
        "percent": percent,
        "items": items,
        "all_complete": done_count == total,
        "done_count": done_count,
        "total": total,
    }
