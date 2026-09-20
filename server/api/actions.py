"""Actions API — sync, reset, export, and other operational endpoints."""

from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.database_pg import DatabasePg
from models.transaction import account_str
from server.access import require_verified_email
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/actions", tags=["actions"])


class SyncRequest(BaseModel):
    sources: list[str] | None = None  # None = all enabled sources
    days: int = 30          # kept for PayPal compat; ignored for Wise when since_date provided
    auto_reconcile: bool = True
    since_date: date | None = None   # Wise: override start date
    until_date: date | None = None   # Wise: override end date (default today)


class SelectProfileRequest(BaseModel):
    profile_id: int


class ResetConfirm(BaseModel):
    confirm: bool = False


@router.get("/wise/profiles")
async def wise_profiles(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List all Wise profiles accessible by the user's token.

    Returns profiles with names and balances so the user can pick which
    business to sync.
    """
    from core.credentials_cloud import CloudCredentialStore
    from core.wise_api import list_wise_profiles

    creds = CloudCredentialStore(db, user.id)
    token = creds.retrieve("wise_api_token")
    if not token:
        raise HTTPException(status_code=400, detail="No Wise token configured — connect Wise in Settings first")

    stored_pid = creds.retrieve("wise_profile_id")
    try:
        profiles = list_wise_profiles(token)
    except Exception as e:
        logger.error("list_wise_profiles failed: %s", e, exc_info=True)
        profiles = []

    return {
        "profiles": profiles or [],
        "selected_profile_id": int(stored_pid) if stored_pid else None,
    }


@router.post("/wise/select-profile")
async def wise_select_profile(
    req: SelectProfileRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Store the user's chosen Wise profile ID for syncing."""
    from core.credentials_cloud import CloudCredentialStore

    creds = CloudCredentialStore(db, user.id)
    creds.store("wise_profile_id", str(req.profile_id))

    return {"status": "ok", "profile_id": req.profile_id}


@router.post("/sync")
async def sync_sources(
    req: SyncRequest,
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Trigger sync from configured sources (Wise, Gmail).

    This runs synchronously for now. Progress updates are sent via SSE.
    """
    results = {}
    sources = req.sources or ["wise"]

    # Send SSE progress
    def _progress(msg: str):
        try:
            from server.api.events import send_event
            send_event(user.id, "sync_progress", {"message": msg})
        except Exception:
            pass

    def _debug(msg: str):
        _progress(msg)
        logger.info("[WISE-SYNC] %s", msg)

    if "wise" in sources:
        try:
            from core.credentials_cloud import CloudCredentialStore
            from core.wise_api import get_profile_id, sync_wise_by_month

            creds = CloudCredentialStore(db, user.id)
            token = creds.retrieve("wise_api_token")
            _debug(f"Token retrieved: {'yes' if token else 'NO'}")

            if token:
                # Use stored profile ID if available (from onboarding test)
                stored_pid_str = creds.retrieve("wise_profile_id")
                stored_pid = int(stored_pid_str) if stored_pid_str else None
                _debug(f"Getting Wise profile ID (stored={stored_pid})...")
                profile_id = get_profile_id(token, preferred_profile_id=stored_pid)
                _debug(f"Profile ID: {profile_id}")

                # Store the resolved profile ID for future syncs
                if profile_id and str(profile_id) != stored_pid_str:
                    try:
                        creds.store("wise_profile_id", str(profile_id))
                    except Exception:
                        pass
                if profile_id:
                    # Resolve date range: use explicit dates if provided, else fall back to days
                    sync_since = req.since_date
                    sync_until = req.until_date or date.today()
                    if sync_since is None and req.days:
                        sync_since = date.today() - timedelta(days=req.days)

                    _debug(f"Request: since_date={req.since_date}, until_date={req.until_date}, days={req.days}")
                    _debug(f"Resolved: sync_since={sync_since}, sync_until={sync_until}, today={date.today()}")

                    wise_results = sync_wise_by_month(
                        token=token,
                        profile_id=profile_id,
                        db=db,
                        user_id=user.id,
                        progress_callback=_debug,
                        since_date=sync_since,
                        until_date=sync_until,
                    )
                    _debug(f"sync_wise_by_month returned: {wise_results}")
                    results.update(wise_results)

                    # Update connection health on success
                    try:
                        db.upsert_connection_health("wise", "HEALTHY")
                    except Exception:
                        pass
                else:
                    # No profile selected — user needs to pick one
                    results["wise"] = {"needs_profile_selection": True}
                    _debug("Multiple profiles found — user must select one")
            else:
                results["wise"] = {"skipped": "No Wise token configured"}
                _debug("SKIPPED: No Wise token configured")
        except ImportError as e:
            from server.errors import error_envelope
            results["wise"] = error_envelope("WISE_MODULE_UNAVAILABLE", "Wise module not available")
            _debug(f"IMPORT ERROR: {e}")
        except Exception as e:
            from server.errors import error_envelope
            logger.error("Wise sync failed: %s", e, exc_info=True)
            results["wise"] = error_envelope("WISE_SYNC_FAILED", "Wise sync failed. Please try again.")
            _debug(f"EXCEPTION: {e}")
            try:
                db.upsert_connection_health("wise", "DEGRADED", error_message=str(e))
            except Exception:
                pass

    # Send completion event
    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {"results": results})
    except Exception:
        pass

    # Auto-reconciliation after sync (if new data arrived)
    reconciliation_result = None
    total_new = sum(
        v.get("new", 0) for v in results.values()
        if isinstance(v, dict) and "new" in v
    )
    if req.auto_reconcile and total_new > 0:
        try:
            from server.api.reconciliation import _run_reconciliation_internal
            reconciliation_result = await _run_reconciliation_internal(
                user, db  # uses centralized AUTO_APPROVE_THRESHOLD from config
            )
        except Exception as e:
            logger.warning("Auto-reconciliation failed: %s", e)

    return {
        "status": "ok",
        "results": results,
        "reconciliation": reconciliation_result,
    }


@router.post("/reconcile")
async def trigger_reconcile(
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Start reconciliation in the background with progress tracking.

    Returns immediately. Frontend polls /api/reconciliation/progress for updates.
    """
    from server.api.reconciliation import start_reconciliation_async
    return await start_reconciliation_async(user=user, db=db)


@router.post("/reset")
async def reset_data(
    body: ResetConfirm,
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Reset all data for the current user. Requires confirm=true.

    WARNING: This deletes all transactions, documents, matches, and ledger entries.
    This action cannot be undone.
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Must set confirm=true to reset all data",
        )

    # Audit log before destructive operation
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                       VALUES (%s, 'system', 0, 'RESET_ALL', '{}', '{}', %s)""",
                    (db.user_id, user.id),
                )
    except Exception:
        logger.warning("Could not write audit log for reset")

    # A reset either empties the account or fails loudly. ``reset_all_tables``
    # is atomic, and a failure has to surface as a 500 that names the table: a
    # 200 with a zero count would let a caller go on to reconcile transactions
    # it believed had just been deleted.
    try:
        counts = db.reset_all_tables()
    except Exception as exc:
        logger.exception("Reset failed for user %s", db.user_id)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "RESET_FAILED",
                "message": str(exc),
            },
        ) from exc

    total = sum(counts.values())

    return {
        "status": "ok",
        "deleted": counts,
        "total_rows_deleted": total,
    }


@router.get("/status")
async def system_status(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return system status: counts of transactions, documents, matches."""
    unmatched_txns = db.get_unmatched_transactions()
    unmatched_docs = db.get_unmatched_documents()
    pending = db.get_pending_reviews()

    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = %s",
                (db.user_id,),
            )
            total_txns = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM documents WHERE user_id = %s",
                (db.user_id,),
            )
            total_docs = cur.fetchone()[0]

    return {
        "total_transactions": total_txns,
        "total_documents": total_docs,
        "unmatched_transactions": len(unmatched_txns),
        "unmatched_documents": len(unmatched_docs),
        "pending_reviews": len(pending),
    }


@router.get("/categories")
async def list_categories(
    user: AuthUser = Depends(get_current_user),
):
    """Return the flat list of fixed Canada ISED/GIFI category names (SSOT).

    Thin legacy-compatibility endpoint; the canonical grouped endpoint is
    ``GET /api/transactions/categories``.
    """
    from config.ised_categories import CATEGORY_NAMES
    return {"categories": list(CATEGORY_NAMES)}


@router.get("/missing-receipts")
async def missing_receipts(
    month: int | None = None,
    year: int | None = None,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List transactions that are missing receipts for a given month."""
    if year is None:
        year = date.today().year
    if month is None:
        month = date.today().month

    txns = db.get_transactions_by_month(year, month)
    unmatched = [t for t in txns if t.status.value == "UNMATCHED"]

    return {
        "year": year,
        "month": month,
        "missing": [
            {
                "id": t.id,
                "date": t.date_posted.isoformat(),
                "description": t.description,
                "amount": str(t.amount),
                "currency": t.currency,
                "account": account_str(t.account),
            }
            for t in unmatched
        ],
        "count": len(unmatched),
    }
