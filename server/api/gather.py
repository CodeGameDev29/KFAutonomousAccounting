"""Gmail scan API — trigger scans, poll status, view history.

Endpoints:
- POST /api/gather/scan — Start a Gmail scan with date range
- GET  /api/gather/scan/status — Get active scan status (or latest completed)
- GET  /api/gather/history — List past scan runs
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.database_pg import DatabasePg
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gather", tags=["gather"])


class ScanRequest(BaseModel):
    since_date: str | None = None  # ISO date YYYY-MM-DD
    until_date: str | None = None  # ISO date YYYY-MM-DD


def _get_creds(db: DatabasePg, user_id: str):
    from core.credentials_cloud import CloudCredentialStore
    return CloudCredentialStore(db, user_id)


def _run_scan_in_background(
    pool, user_id: str, run_id: int,
    since: datetime | None, until: datetime | None,
) -> None:
    """Execute the Gmail scan in a background thread.

    Sends SSE events at each phase transition so the frontend can show
    real-time progress without polling.
    """
    db = DatabasePg(pool, user_id)
    creds = _get_creds(db, user_id)

    try:
        from server.api.events import send_event
    except ImportError:
        send_event = None

    def progress_callback(phase: str, detail: str) -> None:
        """Send SSE scan progress events."""
        if send_event:
            send_event(user_id, "scan_progress", {
                "run_id": run_id,
                "phase": phase,
                "detail": detail,
            })

    try:
        from core.gather.gmail import GmailGatherer
        gatherer = GmailGatherer(creds, db, "gather")

        if not gatherer.is_connected():
            db.update_scan_run(run_id, status="FAILED", error_message="Gmail not connected")
            if send_event:
                send_event(user_id, "scan_failed", {
                    "run_id": run_id,
                    "error": "Gmail not connected",
                })
            return

        progress_callback("search", "Searching your inbox...")
        result = gatherer.gather(since=since, until=until, progress_callback=progress_callback)

        # Count total emails scanned across all passes
        total_emails = (
            result.documents_new
            + result.documents_duplicate
            + result.documents_filtered
        )

        db.update_scan_run(
            run_id,
            status="COMPLETED",
            emails_scanned=total_emails,
            documents_found=result.documents_new + result.documents_duplicate,
            documents_added=result.documents_new,
            documents_skipped=result.documents_duplicate + result.documents_filtered,
        )

        # Update gather source last_gathered timestamp
        db.update_gather_source_last_gathered("gmail")

        if send_event:
            send_event(user_id, "scan_complete", {
                "run_id": run_id,
                "emails_scanned": total_emails,
                "documents_found": result.documents_new + result.documents_duplicate,
                "documents_added": result.documents_new,
                "documents_skipped": result.documents_duplicate + result.documents_filtered,
            })
            # Also invalidate receipts so the UI picks up new documents
            send_event(user_id, "receipts_updated", {})

    except Exception as e:
        logger.exception("Gmail scan failed for user %s", user_id[:8])
        db.update_scan_run(run_id, status="FAILED", error_message=str(e)[:500])
        if send_event:
            send_event(user_id, "scan_failed", {
                "run_id": run_id,
                "error": str(e)[:200],
            })


@router.post("/scan")
async def start_scan(
    req: ScanRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Start a Gmail email scan with optional date range.

    Runs asynchronously in a background thread. Progress is reported
    via SSE events (scan_progress, scan_complete, scan_failed).
    """
    # Check for already-running scan
    active = db.get_active_scan_run()
    if active:
        raise HTTPException(
            status_code=409,
            detail="A scan is already running. Wait for it to complete.",
        )

    # Check Gmail is connected
    creds = _get_creds(db, user.id)
    if not creds.retrieve("gmail_refresh_token"):
        raise HTTPException(status_code=400, detail="Gmail not connected")

    # Parse dates
    since = None
    until = None
    if req.since_date:
        try:
            since = datetime.fromisoformat(req.since_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid since_date format")
    if req.until_date:
        try:
            until = datetime.fromisoformat(req.until_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid until_date format")

    # Create scan run record
    run_id = db.insert_scan_run(
        source="gmail",
        since_date=since,
        until_date=until,
    )

    # Launch background thread
    from db.connection import get_pool
    pool = get_pool()
    thread = threading.Thread(
        target=_run_scan_in_background,
        args=(pool, user.id, run_id, since, until),
        daemon=True,
    )
    thread.start()

    return {
        "status": "started",
        "run_id": run_id,
        "message": "Scan started. Progress will be sent via SSE events.",
    }


@router.get("/scan/status")
async def scan_status(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get the status of the active scan, or the most recent completed scan."""
    active = db.get_active_scan_run()
    if active:
        return {"scan": active, "is_active": True}

    # Return the most recent scan run
    runs = db.get_scan_runs(limit=1)
    if runs:
        return {"scan": runs[0], "is_active": False}

    return {"scan": None, "is_active": False}


@router.get("/history")
async def scan_history(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
    limit: int = 20,
):
    """List past scan runs, most recent first."""
    runs = db.get_scan_runs(limit=min(limit, 50))
    return {"runs": runs}
