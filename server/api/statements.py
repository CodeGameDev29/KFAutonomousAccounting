"""Statement management API endpoints.

Exposes the ``statements`` table the LLM bank-statement parser writes. The
upload flow lives in transactions.py — these endpoints are for browsing,
reviewing, approving and rejecting parsed statements.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from db.database_pg import DatabasePg
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/statements", tags=["statements"])


class ApproveRejectRequest(BaseModel):
    transaction_ids: list[int] | None = None


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


@router.get("")
async def list_statements(
    institution: str | None = None,
    confidence: str | None = None,
    limit: int = 100,
    offset: int = 0,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return all statements for the authenticated user, newest first.

    Optional filters: institution (the bank or provider name), confidence
    ("HIGH", "MEDIUM", "LOW").
    """
    rows = db.list_statements(
        user.id,
        institution=institution,
        confidence=confidence,
        limit=limit,
        offset=offset,
    )
    return rows  # list of dicts — serialised directly as JSON


@router.get("/{statement_id}")
async def get_statement(
    statement_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return a single statement row by UUID."""
    row = db.get_statement_by_id(statement_id, user.id)
    if not row:
        raise HTTPException(status_code=404, detail="Statement not found")
    return row


# ---------------------------------------------------------------------------
# Transactions scoped to a statement
# ---------------------------------------------------------------------------


@router.get("/{statement_id}/transactions")
async def get_statement_transactions(
    statement_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return all PENDING_REVIEW transactions belonging to a statement.

    Note: this endpoint uses ``get_pending_review_transactions``, which only
    returns rows still in the PENDING_REVIEW state. Transactions that have
    already been approved (promoted to UNMATCHED) or rejected (set to
    EXCLUDED) do not appear here.
    """
    # Verify the statement belongs to this user before listing its transactions.
    stmt = db.get_statement_by_id(statement_id, user.id)
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    txns = db.get_pending_review_transactions(user.id, statement_id=statement_id)
    return [_txn_to_dict(t) for t in txns]


# ---------------------------------------------------------------------------
# Approve / reject
# ---------------------------------------------------------------------------


@router.post("/{statement_id}/approve")
async def approve_statement(
    statement_id: str,
    body: ApproveRejectRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Approve statement transactions — promotes PENDING_REVIEW → UNMATCHED.

    If ``transaction_ids`` is omitted or null, all PENDING_REVIEW transactions
    for this statement are approved.
    """
    stmt = db.get_statement_by_id(statement_id, user.id)
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    ids = body.transaction_ids
    if ids is None:
        # approve all pending for this statement
        pending = db.get_pending_review_transactions(user.id, statement_id=statement_id)
        ids = [t.id for t in pending if t.id is not None]

    count = db.promote_transactions_to_imported(user.id, ids) if ids else 0

    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {"source_file": stmt.get("source_file")})
    except Exception:
        logger.debug("Failed to send transactions_updated event after approve", exc_info=True)

    return {"approved": count}


@router.post("/{statement_id}/reject")
async def reject_statement(
    statement_id: str,
    body: ApproveRejectRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Reject specific transactions — sets status to EXCLUDED.

    Rejecting at import review means "this row isn't real" (a parse artifact, a
    duplicate, a line that belongs to somebody else), so its money must never
    reach a total, a chart, the ledger or a tax figure. That is EXCLUDED, not
    IGNORED — IGNORED means "real money, just needs no receipt" and its dollars
    keep counting. The two must never be collapsed into one status.

    ``transaction_ids`` is required for reject; call approve with no IDs to
    approve-all instead.
    """
    if not body.transaction_ids:
        raise HTTPException(
            status_code=400,
            detail="transaction_ids is required for reject; provide an array of integer IDs.",
        )

    # Verify statement ownership before mutating transactions.
    stmt = db.get_statement_by_id(statement_id, user.id)
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    rejected = 0
    for tid in body.transaction_ids:
        try:
            db.update_transaction_status(tid, "EXCLUDED")
            rejected += 1
        except Exception:
            logger.debug("Failed to reject transaction %s", tid, exc_info=True)

    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {"source_file": stmt.get("source_file")})
    except Exception:
        logger.debug("Failed to send transactions_updated event after reject", exc_info=True)

    return {"rejected": rejected}


# ---------------------------------------------------------------------------
# Source PDF preview
# ---------------------------------------------------------------------------


@router.get("/{statement_id}/source-pdf")
async def get_statement_source_pdf(
    statement_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Redirect to a signed URL for the original statement PDF.

    The frontend falls back gracefully on a 404, so returning 404 when the
    file is unavailable is safe.
    """
    stmt = db.get_statement_by_id(statement_id, user.id)
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    source_file: str | None = stmt.get("source_file")
    if not source_file:
        raise HTTPException(status_code=404, detail="Source PDF not available")

    try:
        stored_path: str | None = db.get_processed_file_stored_path(source_file)
        if stored_path:
            from core import file_storage
            signed_url = file_storage.get_download_url(stored_path, source_file)
            return RedirectResponse(url=signed_url)
    except Exception:
        logger.debug(
            "Failed to retrieve signed URL for statement source PDF %s",
            source_file,
            exc_info=True,
        )

    raise HTTPException(status_code=404, detail="Source PDF not available")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _txn_to_dict(t) -> dict:
    """Serialise a Transaction model (or extended row) to a JSON-safe dict."""
    # account may be an AccountType member or a plain institution name.
    account_val = t.account.value if hasattr(t.account, "value") else (t.account or "")
    return {
        "id": t.id,
        "account": account_val,
        "date_posted": (
            t.date_posted.isoformat()
            if hasattr(t.date_posted, "isoformat")
            else t.date_posted
        ),
        "amount": str(t.amount),
        "currency": t.currency,
        "description": t.description,
        "status": t.status.value if hasattr(t.status, "value") else t.status,
        "institution": getattr(t, "institution", None),
        "account_type": getattr(t, "account_type", None),
        "import_confidence": (
            t.import_confidence.value
            if hasattr(t.import_confidence, "value")
            else t.import_confidence
        ) if getattr(t, "import_confidence", None) is not None else None,
        "import_flags": list(getattr(t, "import_flags", None) or []),
        "source_file": getattr(t, "source_file", None),
        "external_id": getattr(t, "external_id", None),
    }
