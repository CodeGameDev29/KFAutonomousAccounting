"""The document queue, as seen by the person who dropped the files in.

Every upload is a row in ``processing_jobs`` that
``server/jobs/document_queue.py`` drains. This router is how the browser watches
that happen and intervenes: list, inspect, retry a failure, cancel something
that has not started, clear the finished ones out of the way.

SSE (``job_updated`` / ``jobs_summary``) is the fast path and this is the source
of truth: a tab that was closed, a reconnect, a phone picking the session back
up hours later all need the same answer without having seen the events, and the
frontend polls ``GET /api/jobs`` on a cadence for exactly that reason.

Every read and write is scoped to the signed-in user through ``DatabasePg``,
which arms RLS — the same posture as every other router here.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from db.database_pg import REROUTABLE_JOB_STATUSES, DatabasePg
from server.auth import AuthUser, get_current_user
from server.deps import get_db
from server.jobs.document_queue import (
    ALL_STATUSES,
    emit_job_event,
    job_public,
    wake_worker,
    worker_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(
    status: str | None = Query(None, description="Filter to one job status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """This user's jobs, their counts, and what the worker is doing.

    ``counts`` is always the whole user's queue, never the filtered page — the
    header that counts running and queued jobs must not change because the list
    below it is filtered. ``batch`` is the same idea over time: the run the user
    is watching, so a reload or a second tab shows the same finished-of-total.
    """
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown status {status!r}. One of: {', '.join(ALL_STATUSES)}",
        )

    jobs = db.list_processing_jobs(status=status, limit=limit, offset=offset)
    return {
        "jobs": [job_public(job) for job in jobs],
        "counts": db.count_processing_jobs(),
        "worker": await worker_status(db),
        # Everything dropped since this queue was last empty. The browser cannot
        # work this out for itself - it only knows the rows it has seen since
        # it loaded - so the finished-of-total count is derived here, where
        # a reload and a second tab get the same answer.
        "batch": db.processing_job_batch(),
    }


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    job = db.get_processing_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_public(job)


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Send a failed job back to the queue with a clean slate.

    Attempts reset to zero, position kept: a retry is the same upload having
    another go, not a new one that jumps the line. The file is still in the
    store, so nothing has to be re-uploaded.
    """
    existing = db.get_processing_job(job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Job not found")

    job = db.retry_processing_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=409,
            detail=f"Only a failed or cancelled job can be retried (this one is "
                   f"{existing['status']}).",
        )
    emit_job_event(job)
    wake_worker()
    logger.info("Job %s retried by its owner", job_id[:8])
    return job_public(job)


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Drop a job that has not started yet.

    Queued only, on purpose: a running job is holding an LLM call and half a
    document's worth of writes, and there is no point in it where stopping would
    leave the books in a state anyone wants.
    """
    existing = db.get_processing_job(job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Job not found")

    job = db.cancel_processing_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=409,
            detail=f"Only a queued job can be cancelled (this one is "
                   f"{existing['status']}).",
        )
    emit_job_event(job)

    # A statement's row is created at upload and sits in 'processing' until its
    # job finishes. Cancelling the job has to close that row too, or the
    # transactions page keeps a statement spinning until the next restart —
    # exactly the kind of apparent hang this queue exists to remove.
    statement_id = (job.get("result") or {}).get("statement_id")
    if job["kind"] == "statement" and statement_id:
        from server.api.transactions import fail_statement_row
        fail_statement_row(
            db, user.id, str(statement_id), job["filename"],
            error_code="CANCELLED",
            message="Upload cancelled before it was parsed.",
        )

    logger.info("Job %s cancelled by its owner", job_id[:8])
    return job_public(job)


@router.post("/{job_id}/reroute")
async def reroute_job_to_receipt(
    job_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """"No, it's a receipt." One click, no re-upload.

    The Transactions "auto" zone sniffs a dropped PDF and sends a bank
    statement down the statement pipeline (``core/statement_detector``, marked
    ``result.detected_as = "statement"``). The detector is strict, but nothing
    that guesses is always right, so the guess has to be cheap to undo: the file
    is already in the store, so re-filing it as a receipt is a swap of the queue
    row rather than a second upload and a second extraction of the same bytes.

    What happens, in order:

    * the ``statements`` row the statement path opened is closed out as
      cancelled — leaving it would spin on the transactions page forever — and
      announced as ``statement_rerouted``, **not** as the
      ``statement_failed`` / ``statement_parse_error`` pair. Nothing failed: the
      user has said the file was never a statement, and a red "could not read
      this statement" card is the wrong answer to that;
    * the statement job is deleted (not cancelled: one dropped file must not read
      as two rows in the queue panel);
    * a receipt job is inserted against the same ``stored_path`` / ``file_hash``,
      carrying ``result.rerouted_from`` so the trail survives;
    * the browser hears it — ``job_updated`` for the new job, ``jobs_summary``
      for every other tab, whose old row is named by ``replaced_job_id``.

    Refused (409) once the job is past the point of changing its mind: a
    ``running`` job is mid-LLM-call and a ``done`` one has already written
    transactions to the ledger, which is an undo, not a reroute.
    """
    existing = db.get_processing_job(job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if existing["kind"] != "statement":
        raise HTTPException(
            status_code=409,
            detail="Only a statement upload can be re-filed as a receipt "
                   f"(this one is already a {existing['kind']}).",
        )

    status = existing["status"]
    if status not in REROUTABLE_JOB_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"This upload is already {status} and cannot be re-filed as a "
                   "receipt. Delete the imported transactions and upload the file "
                   "again instead.",
        )

    stored_path = existing.get("stored_path")
    if not stored_path:
        raise HTTPException(
            status_code=409,
            detail="This upload was never stored, so there is no file to re-file. "
                   "Upload it again.",
        )

    result = existing.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            result = None
    statement_id = (result or {}).get("statement_id")

    # Close the statements row first. If the insert below fails, a cancelled
    # statement and no receipt job (the user retries) is a better outcome than a
    # statement stuck in 'processing' with nothing left to finish it.
    if statement_id:
        from server.api.transactions import reroute_statement_row
        reroute_statement_row(
            db, user.id, str(statement_id), existing["filename"],
            message="Re-filed as a receipt, so this statement import was cancelled.",
        )

    if not db.delete_processing_job(job_id):
        # Lost a race with the worker or another tab between the read and here.
        raise HTTPException(
            status_code=409,
            detail="This upload started processing before it could be re-filed. "
                   "Wait for it to finish.",
        )

    job = db.insert_processing_job(
        kind="receipt",
        filename=existing["filename"],
        stored_path=str(stored_path),
        file_hash=existing.get("file_hash"),
        pages_total=existing.get("pages_total"),
        result={"rerouted_from": "statement", "rerouted_from_job_id": str(job_id)},
    )
    emit_job_event(job)
    wake_worker()

    # The deleted row is gone from every other tab's list too, and clearing it
    # re-bases the batch, so the counts have to be re-broadcast the same way
    # ``clear_jobs`` does it.
    try:
        from server.api.events import send_event

        send_event(
            user.id,
            "jobs_summary",
            {**db.count_processing_jobs(), "batch": db.processing_job_batch()},
        )
    except Exception:
        logger.debug("jobs_summary SSE failed after reroute", exc_info=True)

    logger.info(
        "Job %s (statement) re-filed as receipt job %s at position %s",
        str(job_id)[:8], str(job["id"])[:8], job["position"],
    )
    body = job_public(job) or {}
    body["replaced_job_id"] = str(job_id)
    return body


@router.delete("")
async def clear_jobs(
    status: str = Query("done", description="Which finished jobs to clear"),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Clear finished jobs off the list. Pending work is never touched."""
    if status not in ("done", "failed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail="Only finished jobs can be cleared: done, failed or cancelled.",
        )
    deleted = db.delete_processing_jobs(status)
    counts = db.count_processing_jobs()
    batch = db.processing_job_batch()
    try:
        from server.api.events import send_event

        # Clearing re-bases the batch (the anchor moves to the oldest row still
        # on the list), so every other tab has to hear the new total, not just
        # the tab that pressed the button.
        send_event(user.id, "jobs_summary", {**counts, "batch": batch})
    except Exception:
        logger.debug("jobs_summary SSE failed after clear", exc_info=True)
    return {"deleted": deleted, "status": status, "counts": counts}
