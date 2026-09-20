"""Receipt upload and management API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from psycopg2.errors import UniqueViolation
from pydantic import BaseModel

from config.settings import MAX_UPLOAD_BYTES, MAX_UPLOAD_MB
from core.document_currency import INFERRED as CURRENCY_INFERRED
from core.document_currency import currency_from_extraction
from db.database_pg import DatabasePg
from server.access import require_verified_email
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/receipts", tags=["receipts"])

SUPPORTED_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp",
    ".gif", ".bmp", ".tiff", ".tif",
    ".xlsx", ".xls", ".csv",
    ".docx", ".doc",
}

# mimetypes.guess_type() misses Office formats on some platforms — register them
mimetypes.add_type("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")
mimetypes.add_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")
mimetypes.add_type("application/msword", ".doc")
mimetypes.add_type("application/vnd.ms-excel", ".xls")
# One cap for every upload route — see config.settings.MAX_UPLOAD_MB.
MAX_FILE_SIZE = MAX_UPLOAD_BYTES

# Magic byte signatures for file type validation (CWE-434)
_MAGIC_BYTES = {
    ".pdf": [b"%PDF"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".jfif": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".webp": [b"RIFF"],  # RIFF....WEBP
    ".gif": [b"GIF87a", b"GIF89a"],
    ".bmp": [b"BM"],
    ".tiff": [b"II\x2a\x00", b"MM\x00\x2a"],  # Little-endian / big-endian TIFF
    ".tif": [b"II\x2a\x00", b"MM\x00\x2a"],
    ".xlsx": [b"PK"],  # ZIP-based Office Open XML
    ".xls": [b"\xd0\xcf\x11\xe0"],  # OLE2 compound document
    ".docx": [b"PK"],  # ZIP-based Office Open XML
    ".doc": [b"\xd0\xcf\x11\xe0"],  # OLE2 compound document
    # .csv has no magic bytes — skip validation for it
}


def _compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _total_number(total) -> float | None:
    """A money amount as a JSON **number**, two decimal places.

    Decimal → float happens here, at the JSON boundary, and nowhere else, so
    every route reports an amount as the same JSON type. A client that has to
    coerce a string before formatting will eventually forget and render a bare
    figure with no currency (``String.prototype.toLocaleString`` ignores
    currency options on a string).
    """
    if total is None or total == "":
        return None
    try:
        return round(float(str(total).replace("$", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _duplicate_payload(existing_doc: dict, filename: str) -> dict:
    """The 200 body that says "you already have this file, here it is".

    Shared by the up-front hash guard and the conflict-on-insert path so a
    duplicate resolves to one document and one response shape either way.
    """
    return {
        "status": "ok",
        "doc_id": existing_doc["id"],
        "vendor": existing_doc.get("vendor"),
        "date": existing_doc.get("document_date"),
        "total": _total_number(existing_doc.get("total")),
        "currency": existing_doc.get("currency"),
        "confidence": existing_doc.get("extraction_confidence"),
        "duplicate": True,
        "filename": filename,
    }


async def _reroute_pdf_to_statement(
    *,
    db: DatabasePg,
    user: AuthUser,
    filename: str,
    file_bytes: bytes,
    file_hash: str,
    content_type: str | None,
) -> JSONResponse | None:
    """The auto zone's one real job: don't extract a bank statement as a receipt.

    Returns the statement path's 202 when the PDF is recognised as a statement,
    and ``None`` when it is not — in which case the caller carries on down the
    ordinary receipt flow, untouched.

    Routing on file extension alone would send every PDF in a dropped folder
    down the receipt path, and a bank statement read as a receipt collapses into
    one vendor and one total with none of its rows imported.
    ``core/statement_detector`` reads the text layer of the first few pages — no
    LLM, no rendering, no network — and this hands a recognised statement to the
    exact same code ``POST /api/transactions/upload-statement`` runs
    (``_queue_statement_pdf``), so the queue gets a ``kind="statement"`` job with
    a ``statements`` row behind it rather than a receipt one.
    """
    from core.statement_detector import detect_bank_statement

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        detection = await asyncio.to_thread(detect_bank_statement, tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Logged either way. A misfire — a receipt sent to the statement pipeline, or
    # a statement still read as a receipt — is only debuggable if the evidence
    # that decided it is on disk.
    logger.info("Auto-zone sniff of %s: %s", filename, detection.summary)
    if not detection.is_statement:
        return None

    from server.api.transactions import (
        DETECTED_AS_STATEMENT,
        _queue_statement_pdf,
        replace_prior_statement_data,
        statement_duplicate_response,
    )

    # The same bytes already queued as a statement are the same upload twice —
    # a folder dropped again, a double-click on the zone. Answer with the job the
    # first one made; already-imported bytes are a 409 from the same helper.
    duplicate = statement_duplicate_response(
        db,
        filename=filename,
        file_hash=file_hash,
        # The auto zone's 202 always carries these two, duplicate or not, so the
        # browser can still offer "no, it's a receipt" on the job it gets back.
        extra={"detected_as": DETECTED_AS_STATEMENT, "reroute_url": True},
    )
    if duplicate is not None:
        return duplicate

    replaced_count = replace_prior_statement_data(db, filename)

    return await _queue_statement_pdf(
        db=db,
        user=user,
        filename=filename,
        file_bytes=file_bytes,
        file_hash=file_hash,
        content_type=content_type,
        replaced_count=replaced_count,
        detected_evidence=detection.as_dict(),
    )


@router.post("/upload", status_code=202)
async def upload_receipt(
    file: UploadFile = File(...),
    auto_detect: bool = Query(
        False,
        description="Sniff a PDF and route it to the statement pipeline if it is "
                    "one. Off by default so the explicit Upload Receipts zone is "
                    "never second-guessed.",
    ),
    auto_detect_field: bool | None = Form(None, alias="auto_detect"),
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Accept a receipt and queue it for extraction. Answers 202 immediately.

    Extraction takes far longer than a request timeout allows, and anything
    done inside the request is lost if the process restarts. So the request ends
    at "stored and queued" — quickly, whether one file was dropped or hundreds —
    and ``server/jobs/document_queue.py`` does the work, with every pending
    upload in one FIFO line.

    ``auto_detect`` (query parameter ``?auto_detect=1``, or a multipart form field
    of the same name — either is honoured) says "I am the Transactions auto zone,
    I do not know what this file is". With it on, a PDF that
    ``core/statement_detector`` recognises as a bank statement takes the statement
    path instead of being extracted as a receipt, and the 202 says so. It is off by
    default: a file dropped in the explicit "Upload Receipts" zone is a receipt
    because the user said it was, and is never second-guessed.

    Flow:
    1. Pre-flight: extension + size + magic-byte validation
    2. ``auto_detect`` only: sniff a PDF; a statement leaves down the statement
       path and never reaches the receipt flow below. Deliberately *before* the
       duplicate checks — those are keyed on receipt rows, so a statement that
       was once mis-extracted as a receipt would otherwise be answered "you
       already have this" and stay wrong forever
    3. Duplicate check — an existing document, or an identical file already in
       the queue; either returns what exists
    4. Store the file durably (the worker may not reach this job until long
       after the request, or until after a restart, so a temp file will not do)
    5. Insert the job, wake the worker, answer 202
    """
    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower()

    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB}MB)"
        )

    # Validate magic bytes match declared file extension
    expected_magic = _MAGIC_BYTES.get(ext, [])
    if expected_magic and not any(file_bytes[:len(m)] == m for m in expected_magic):
        raise HTTPException(
            status_code=400,
            detail=f"File content does not match '{ext}' format",
        )

    file_hash = _compute_hash(file_bytes)

    # The auto zone does not know what it is holding. Give it the statement
    # pipeline when the file is one, before anything here treats it as a receipt.
    if (auto_detect or auto_detect_field) and ext == ".pdf":
        rerouted = await _reroute_pdf_to_statement(
            db=db,
            user=user,
            filename=filename,
            file_bytes=file_bytes,
            file_hash=file_hash,
            content_type=file.content_type,
        )
        if rerouted is not None:
            return rerouted

    # Duplicate detection — if the same file content was already uploaded and
    # extracted, return the existing result immediately.
    existing_doc = db.get_document_by_hash(file_hash)
    if existing_doc is not None:
        logger.info("Duplicate file hash=%s already extracted as doc %s, returning existing result",
                     file_hash[:12], existing_doc["id"])
        return JSONResponse(status_code=200, content=_duplicate_payload(existing_doc, filename))

    # The same bytes already waiting in the queue are also a duplicate —
    # dropping a folder twice must not extract the same file twice. The answer
    # is the job the first upload created.
    try:
        active_job = db.find_active_processing_job_by_hash("receipt", file_hash)
    except Exception:
        logger.debug("in-flight duplicate check failed for %s", filename, exc_info=True)
        active_job = None
    if active_job is not None:
        from server.jobs.document_queue import job_public
        body = job_public(active_job) or {}
        body["duplicate"] = True
        logger.info(
            "Duplicate upload of %s (hash=%s) — already queued as job %s",
            filename, file_hash[:12], str(active_job["id"])[:8],
        )
        return JSONResponse(status_code=202, content=body)

    # The worker reads the file off disk whenever its turn comes — possibly
    # after a restart — so the durable copy has to exist before the job does.
    try:
        from core.file_storage import upload_file as storage_upload
        stored_path = storage_upload(user.id, file_bytes, filename, file.content_type)
    except Exception as storage_exc:
        logger.error("Storage upload failed for receipt %s: %s", filename, storage_exc)
        raise HTTPException(
            status_code=503,
            detail="File storage unavailable. Please try again later.",
        )

    pages_total = await _count_pdf_pages(file_bytes) if ext == ".pdf" else None

    job = db.insert_processing_job(
        kind="receipt",
        filename=filename,
        stored_path=stored_path,
        file_hash=file_hash,
        pages_total=pages_total,
    )

    from server.jobs.document_queue import emit_job_event, wake_worker
    emit_job_event(job)
    wake_worker()

    logger.info(
        "Receipt %s queued as job %s at position %s",
        filename, str(job["id"])[:8], job["position"],
    )
    from server.jobs.document_queue import job_public
    return JSONResponse(status_code=202, content=job_public(job))


async def _count_pdf_pages(file_bytes: bytes) -> int | None:
    """Page count for the queue's progress display. Best-effort, never raises.

    Knowing this at accept time is what lets the UI say which page of how many
    is being read from the first second, rather than only once the model has
    answered.
    """
    def _count() -> int | None:
        try:
            import fitz  # PyMuPDF, already a hard dependency of extraction

            with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                return int(doc.page_count)
        except Exception:
            return None

    try:
        import asyncio
        return await asyncio.to_thread(_count)
    except Exception:
        return None


def _receipt_summary(
    *,
    vendor: str | None,
    document_date: str | None,
    total: str | None,
    currency: str | None,
    confidence: str | None,
    prefix: str | None = None,
) -> dict:
    """The ``result.summary`` block a finished receipt job carries.

    Shape is the queue's, not the document's: whatever the engine knew, plus a
    ``text`` one-liner the UI can render as-is without re-deriving it.

    ``text`` is composed in **ASCII**, deliberately. A machine-written line
    travels further than the browser: it is written to the server log and read
    back by tooling, and a JSON body served with no charset is decoded as
    latin-1 by some clients, which turns a non-ASCII separator into mojibake.
    The separator carries no meaning, so it does not get to break those readers.
    """
    total_number = _total_number(total)
    money = None
    if total_number is not None:
        money = f"{currency + ' ' if currency else ''}{total_number:,.2f}"
    text = " - ".join(
        part for part in [prefix, vendor or "Unknown vendor", money] if part
    )
    return {
        "vendor": vendor,
        "date": str(document_date) if document_date else None,
        "total": total_number,
        "currency": currency,
        "confidence": confidence,
        "text": text,
    }


def review_status_for(field_confidence: dict, extraction_data: dict) -> str:
    """``auto_accepted`` / ``pending`` / ``flagged`` for a fresh extraction.

    The plain rule: any ``low`` field flags the document for a human, and
    everything must be ``high`` to skip the review queue altogether.

    The one exception is the currency, and it exists because ``medium`` there
    says two different things (``core/document_currency.py``). A currency read
    off the document's own Canadian tax line — the GST/HST/PST field or a
    5/12/13/14/15% tax ratio, stored as ``currency_source='inferred'`` — is
    *evidence*, not doubt. It is graded medium so the review panel can say why a
    receipt that prints a bare ``$`` is CAD; it is not graded medium because
    anyone is unsure that it is. Sending every such receipt to review would
    queue up work that the document itself already answers, so an inferred
    currency is accept-grade here while the confidence blob keeps saying medium.

    An **assumed** currency — no marker printed, no tax line, nothing written
    down anywhere — grades ``low`` and still flags. And a ``low`` or ``medium``
    on any other field still keeps the document out of ``auto_accepted``.

    The source is read with the same :func:`currency_from_extraction` that
    :func:`core.extraction.build_document_from_extraction` uses on the same
    payload, so it is by construction the ``currency_source`` this document is
    about to be stored with.
    """
    grades = dict(field_confidence or {})
    if grades.get("currency") == "medium":
        try:
            _code, source = currency_from_extraction(extraction_data)
        except Exception:  # pragma: no cover - defensive; the helper never raises
            source = None
        if source == CURRENCY_INFERRED:
            grades["currency"] = "high"

    if any(v == "low" for v in grades.values()):
        return "flagged"
    if all(v == "high" for v in grades.values()):
        return "auto_accepted"
    return "pending"


async def process_receipt_document(
    *,
    db: DatabasePg,
    user_id: str,
    filename: str,
    file_hash: str,
    file_path: Path,
    stored_path: str,
    progress_cb=None,
) -> dict:
    """Extract one queued receipt and persist it — the engine path.

    Called by the document queue, never from inside a request, which shapes two
    things:

    * the file is already in the store (the job row points at it), so there is
      no upload step here and no temp file to clean up;
    * failures are **raised**, not turned into an HTTP status. The queue
      classifies them (``server/jobs/document_queue.classify_job_failure``) and
      decides whether that is a retry, a wait for the LLM endpoint, or a dead
      job.

    ``progress_cb(pages_done, pages_total)`` is forwarded to the engine when the
    engine's signature accepts it, so page-batching can light up the progress
    bar without this file changing again.

    Returns the ``result`` written onto the job row.
    """
    from core.extraction import (
        build_document_from_extraction,
        compute_field_confidence,
        extract_document,
        record_extraction_attempts,
    )

    extra: dict = {}
    if progress_cb is not None:
        from server.jobs.document_queue import engine_accepts_progress_cb
        if engine_accepts_progress_cb(extract_document):
            extra["progress_cb"] = progress_cb

    try:
        # Pass db so the extraction's token usage lands in api_calls, whichever
        # provider answered; without it the usage ledger shows no extraction
        # activity at all.
        extraction_data = await extract_document(file_path, db=db, **extra)
        provider = extraction_data.get("_provider", "unknown")

        # Non-financial document. The extraction call has already run and its
        # answer is recorded, so the file is marked processed rather than left
        # for the pipeline to read again on the next upload.
        #
        # This branch writes to processed_files and never to documents, so a
        # re-upload of the same non-financial file gets past the hash guard in
        # the endpoint (there is no document row to find) and lands right back
        # here on the same (user_id, file_hash). insert_processed_file is ON
        # CONFLICT for exactly that reason — otherwise it raises UniqueViolation
        # and turns a finished extraction into a 500.
        if extraction_data.get("vendor") == "NOT_A_FINANCIAL_DOCUMENT":
            db.insert_processed_file(file_hash, filename, stored_path=stored_path)
            return {
                "skipped": True,
                "reason": "Not a financial document",
                "summary": {"text": "Not a financial document"},
            }

        # Compute per-field confidence
        field_confidence = compute_field_confidence(extraction_data)

        # Determine review_status based on field confidence
        review_status = review_status_for(field_confidence, extraction_data)

        # When the preferred model is out of quota the ladder falls back to a
        # lesser one, and a lesser model reports the same "high" self-confidence
        # while being more likely to misread a vendor or a total — so
        # field_confidence alone would auto-accept a wrong number. Never
        # auto-accept what a fallback rung produced; a human sees it.
        from config.settings import trusted_extraction_models
        trusted = trusted_extraction_models()
        used_model = extraction_data.get("_model")
        if used_model and used_model not in trusted and review_status == "auto_accepted":
            review_status = "pending"
            logger.info(
                "Doc from %s extracted by fallback model %s (trusted: %s) — "
                "held for review instead of auto-accepted",
                filename, used_model, trusted,
            )

        # Build document model
        doc = build_document_from_extraction(
            extraction_data=extraction_data,
            original_filename=filename,
            file_hash=file_hash,
            raw_json=str(extraction_data),
        )
        doc.stored_path = stored_path

        # Record in processed_files (used for cross-endpoint dedup awareness,
        # e.g. same PDF uploaded to both statement and receipt endpoints).
        # Primary receipt dedup is via documents.file_hash in the endpoint. A
        # hash collision here is a duplicate upload, not a failure — it is
        # answered by the UniqueViolation handler below, so it must not be
        # swallowed.
        try:
            db.insert_processed_file(file_hash, filename, stored_path=stored_path)
        except UniqueViolation:
            raise
        except Exception:
            logger.warning("processed_files dedup row not written for %s", filename, exc_info=True)
        doc_id = db.insert_document(doc)

        # Store field_confidence and review_status on the document row
        try:
            with db._conn() as conn_ctx:
                with conn_ctx.cursor() as cur:
                    cur.execute(
                        """UPDATE documents
                           SET field_confidence = %s, review_status = %s
                           WHERE id = %s AND user_id = %s""",
                        (json.dumps(field_confidence), review_status, doc_id, db.user_id),
                    )
        except Exception:
            logger.warning("Failed to store field_confidence for doc_id=%s", doc_id)

        # Phase 3: write per-attempt extraction telemetry. Best-effort.
        try:
            telemetry = extraction_data.get("_telemetry") or []
            if telemetry:
                final_model = extraction_data.get("_model")
                # Use the final attempt's confidence as the persisted numeric confidence.
                final_confidence = telemetry[-1].confidence if telemetry else None
                await record_extraction_attempts(
                    db, user_id, doc_id, telemetry,
                    final_model=final_model,
                    final_confidence=final_confidence,
                )
        except Exception:
            logger.debug("extraction_log write skipped for doc_id=%s", doc_id, exc_info=True)

        # Audit log
        try:
            db.insert_audit_log(
                entity_type="document",
                entity_id=doc_id,
                action="UPLOAD_RECEIPT",
                performed_by=user_id,
                new_value={
                    "filename": filename,
                    "vendor": doc.vendor,
                    "total": str(doc.total) if doc.total else None,
                    "currency": doc.currency,
                    "provider": provider,
                },
            )
        except Exception:
            pass

        # Send SSE event
        try:
            from server.api.events import send_event
            send_event(user_id, "receipts_updated", {"doc_id": doc_id})
        except Exception:
            pass

        total_str = str(doc.total) if doc.total else None
        return {
            "document_id": doc_id,
            "statement_id": None,
            "review_status": review_status,
            "provider": provider,
            "field_confidence": field_confidence,
            "summary": _receipt_summary(
                vendor=doc.vendor,
                document_date=doc.document_date.isoformat() if doc.document_date else None,
                total=total_str,
                currency=doc.currency,
                confidence=doc.extraction_confidence,
            ),
        }

    except UniqueViolation as exc:
        # The same bytes reached an INSERT twice: two uploads in flight together
        # (both cleared the hash guard before either row existed), or a
        # re-upload of a file that never produced a document row. The other
        # job's work is the answer — resolve to the existing document exactly as
        # the up-front dedup path does. Never a failed job: the extraction is
        # already done, and one WARNING line says so without burying a real
        # crash in the server error log under an expected traceback.
        raced_doc = db.get_document_by_hash(file_hash)
        if raced_doc is not None:
            logger.warning(
                "Duplicate upload race on %s (hash=%s) — returning existing doc %s",
                filename, file_hash[:12], raced_doc["id"],
            )
            return {
                "duplicate": True,
                "document_id": raced_doc["id"],
                "summary": _receipt_summary(
                    vendor=raced_doc.get("vendor"),
                    document_date=raced_doc.get("document_date"),
                    total=raced_doc.get("total"),
                    currency=raced_doc.get("currency"),
                    confidence=raced_doc.get("extraction_confidence"),
                    prefix="Already uploaded",
                ),
            }
        logger.warning(
            "Duplicate upload race on %s (hash=%s) with no document row yet (%s)",
            filename, file_hash[:12],
            str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__,
        )
        return {
            "duplicate": True,
            "document_id": None,
            "summary": {"text": "Already being processed as a duplicate upload"},
        }


@router.post("/clear-all")
async def clear_all_documents(
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Purge every proof of transaction and its traces for the current user.

    Wipes documents, reconciliation_matches, ledger_entries, and processed_files
    rows tied to those documents. Used by the "Clear All" control in the
    Transactions UI to reset from scratch.
    """
    uid = user.id
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, file_hash FROM documents WHERE user_id = %s",
                (uid,),
            )
            rows = cur.fetchall()
            if not rows:
                return {"deleted": 0}
            doc_ids = [r[0] for r in rows]
            hashes = [r[1] for r in rows if r[1]]

            cur.execute(
                "DELETE FROM ledger_entries WHERE user_id = %s AND match_id IN "
                "(SELECT id FROM reconciliation_matches WHERE user_id = %s AND document_id = ANY(%s))",
                (uid, uid, doc_ids),
            )
            # Transactions these documents were proving — re-derived after the
            # rows are gone, otherwise they stay MATCHED with no receipt, never
            # re-enter the unmatched pool, and inflate the resolution rate.
            cur.execute(
                """SELECT DISTINCT transaction_id FROM reconciliation_matches
                   WHERE user_id = %s AND document_id = ANY(%s)
                     AND transaction_id IS NOT NULL""",
                (uid, doc_ids),
            )
            affected_txn_ids = [r[0] for r in cur.fetchall()]
            cur.execute(
                "DELETE FROM reconciliation_matches WHERE user_id = %s AND document_id = ANY(%s)",
                (uid, doc_ids),
            )
            db.resync_statuses_in_cursor(cur, transaction_ids=affected_txn_ids)
            cur.execute(
                "DELETE FROM documents WHERE user_id = %s AND id = ANY(%s)",
                (uid, doc_ids),
            )
            deleted = cur.rowcount
            if hashes:
                cur.execute(
                    "DELETE FROM processed_files WHERE user_id = %s AND file_hash = ANY(%s)",
                    (uid, hashes),
                )
            conn.commit()

    try:
        db.insert_audit_log(
            entity_type="document",
            entity_id=0,
            action="DELETE_RECEIPT",
            performed_by=uid,
            old_value=f"all receipts ({deleted} document{'' if deleted == 1 else 's'})",
        )
    except Exception:
        logger.warning("audit_log write failed for DELETE_RECEIPT (clear-all)", exc_info=True)

    try:
        from server.api.events import send_event
        send_event(uid, "receipts_updated", {"cleared_all": True})
    except Exception:
        pass

    return {"deleted": deleted}


@router.delete("/by-hash/{file_hash}")
async def delete_document_by_hash(
    file_hash: str,
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Purge a document by file hash — removes all traces from every table.

    Use when you only have the file content (hash) but not the doc_id.
    """
    db.purge_document_by_hash(file_hash)
    return {"status": "purged", "file_hash": file_hash}


@router.get("")
async def list_documents(
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 50,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List uploaded documents/receipts with optional status filter.

    `search` exhausts every criterion (see DatabasePg.doc_search_predicate):
    the document id (exact), vendor/filename/currency/date (substring), and the
    document total (exact dollar value) — so a receipt is findable by its
    amount, not only its name or ID.
    """
    # Use raw query for pagination
    with db._conn() as conn:
        with conn.cursor() as cur:
            where_clauses = ["user_id = %s"]
            where_params: list = [db.user_id]

            if status:
                where_clauses.append("status = %s")
                where_params.append(status)

            if search and search.strip():
                # Exhaust every criterion — id (exact), vendor/filename/currency/
                # date (substring), and total (exact dollar value). Shared with
                # the "match receipt" dialog so both bars behave identically.
                predicate, p_params = db.doc_search_predicate(search)
                where_clauses.append(predicate)
                where_params.extend(p_params)

            where_sql = " AND ".join(where_clauses)

            # Count unique documents (deduplicated by file_hash)
            cur.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT DISTINCT ON (file_hash) id FROM documents WHERE " + where_sql +
                "  ORDER BY file_hash, id"
                ") AS deduped",
                where_params,
            )
            total = cur.fetchone()[0]

            # Paginated results — deduplicated by file_hash, keeping earliest upload.
            # Matches are AGGREGATED so a doc that covers multiple transactions
            # (ONE_TO_MANY) returns one row, with the first matched txn id and
            # a total count. A LEFT JOIN without the aggregation returns one row
            # per covered transaction, which renders as duplicate cards for a
            # split-coverage doc and leaves the "Matched" badge unclickable when
            # the join lands on a row the status filter excluded.
            offset = (page - 1) * per_page
            cur.execute(
                "SELECT d.id, d.original_filename, d.stored_path, d.vendor, d.document_date,"
                "       d.currency, d.total, d.extraction_confidence, d.status, d.created_at,"
                "       m.first_txn_id, m.txn_count, t.source_file AS matched_source_file,"
                "       m.all_txn_ids"
                " FROM ("
                "  SELECT DISTINCT ON (file_hash)"
                "         id, original_filename, stored_path, file_hash, vendor, document_date,"
                "         currency, total, extraction_confidence, status, created_at"
                "  FROM documents WHERE " + where_sql +
                "  ORDER BY file_hash, id"
                " ) AS d"
                " LEFT JOIN ("
                "   SELECT document_id,"
                "          MIN(transaction_id) AS first_txn_id,"
                "          COUNT(transaction_id) AS txn_count,"
                "          ARRAY_AGG(transaction_id ORDER BY transaction_id) AS all_txn_ids"
                "   FROM reconciliation_matches"
                "   WHERE user_id = %s"
                "     AND status IN ('AUTO_APPROVED', 'USER_APPROVED', 'PENDING_REVIEW')"
                "   GROUP BY document_id"
                " ) AS m ON m.document_id = d.id"
                " LEFT JOIN transactions t"
                "   ON t.id = m.first_txn_id AND t.user_id = %s"
                " ORDER BY d.created_at DESC"
                " LIMIT %s OFFSET %s",
                where_params + [db.user_id, db.user_id, per_page, offset],
            )

            docs = []
            for row in cur.fetchall():
                docs.append({
                    "id": row[0],
                    "filename": row[1],
                    "vendor": row[3],
                    "date": row[4],
                    "currency": row[5],
                    "total": row[6],
                    "confidence": row[7],
                    "status": row[8],
                    "created_at": str(row[9]) if row[9] else None,
                    # First matched transaction id (smallest). Use for jump-to-row.
                    "matched_transaction_id": row[10],
                    # Total count of distinct transactions this doc covers. >1
                    # means split-coverage; UI should show "(N) ↗" badge.
                    "matched_transaction_count": int(row[11]) if row[11] is not None else 0,
                    "matched_source_file": row[12],
                    # Full id list for multi-highlight UI. Empty array if no matches.
                    "matched_transaction_ids": list(row[13]) if row[13] is not None else [],
                })

            return {
                "documents": docs,
                "total": total,
                "page": page,
                "per_page": per_page,
            }


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: int,
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Delete a document and all its traces, preserving the audit chain.

    Resets any matched transactions back to UNMATCHED (so the user sees them
    in the review queue again), deletes reconciliation_matches and ledger
    entries that pointed at this doc, then deletes the document row.
    """
    uid = user.id
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Check document exists
            cur.execute(
                "SELECT id, file_hash, original_filename, vendor FROM documents "
                "WHERE id = %s AND user_id = %s",
                (doc_id, uid),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document not found")
            file_hash = row[1]
            deleted_label = row[2] or f"document {doc_id}"
            if row[3]:
                deleted_label = f"{deleted_label} ({row[3]})"

            # Which transactions this document was proving — re-derived AFTER
            # the match rows are gone (below), so a transaction that still has
            # another proof or an active link keeps its status, and one that
            # has nothing left is not stranded as MATCHED with no proofs.
            cur.execute(
                "SELECT DISTINCT transaction_id FROM reconciliation_matches "
                "WHERE document_id = %s AND user_id = %s AND transaction_id IS NOT NULL",
                (doc_id, uid),
            )
            txn_ids = [r[0] for r in cur.fetchall()]

            # Delete ledger entries referencing the matches about to be dropped
            cur.execute(
                "DELETE FROM ledger_entries WHERE user_id = %s AND match_id IN "
                "(SELECT id FROM reconciliation_matches WHERE document_id = %s AND user_id = %s)",
                (uid, doc_id, uid),
            )

            # Delete matches
            cur.execute(
                "DELETE FROM reconciliation_matches WHERE document_id = %s AND user_id = %s",
                (doc_id, uid),
            )

            # Now that the rows are gone, re-derive the transactions they were
            # proving — same transaction, same rule as everywhere else.
            reverted = db.resync_statuses_in_cursor(cur, transaction_ids=txn_ids)

            # Delete processed file entry (allows re-upload of same content)
            if file_hash:
                cur.execute(
                    "DELETE FROM processed_files WHERE user_id = %s AND file_hash = %s",
                    (uid, file_hash),
                )

            # Delete the document itself
            cur.execute("DELETE FROM documents WHERE id = %s AND user_id = %s", (doc_id, uid))

            conn.commit()

    # Deleting a receipt removes the evidence behind a reconciled row, so it
    # belongs in the audit trail with what went and what it freed.
    freed = len(reverted["transactions"])
    if freed:
        deleted_label += (
            f" — {freed} transaction{'' if freed == 1 else 's'} back to unmatched"
        )
    try:
        db.insert_audit_log(
            entity_type="document",
            entity_id=doc_id,
            action="DELETE_RECEIPT",
            performed_by=uid,
            old_value=deleted_label,
        )
    except Exception:
        logger.warning("audit_log write failed for DELETE_RECEIPT %s", doc_id, exc_info=True)

    # Invalidate caches via SSE
    try:
        from server.api.events import send_event
        send_event(user.id, "receipts_updated", {"purged_doc_id": doc_id})
    except Exception:
        pass

    return {"status": "purged", "doc_id": doc_id, "deleted": True}


@router.get("/{doc_id}")
async def get_document(
    doc_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get full document details including extraction data and line items."""
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, original_filename, stored_path, file_hash, vendor,
                          document_date, currency, subtotal, tax_gst, tax_hst, tax_pst,
                          tax_other, total, payment_method, invoice_number,
                          line_items_json, extraction_confidence, extraction_raw_json,
                          status, created_at
                   FROM documents WHERE id = %s AND user_id = %s""",
                (doc_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document not found")

            import json
            line_items = json.loads(row[15]) if row[15] else None

            return {
                "id": row[0],
                "filename": row[1],
                "stored_path": row[2],
                "file_hash": row[3],
                "vendor": row[4],
                "date": row[5],
                "currency": row[6],
                "subtotal": row[7],
                "tax_gst": row[8],
                "tax_hst": row[9],
                "tax_pst": row[10],
                "tax_other": row[11],
                "total": row[12],
                "payment_method": row[13],
                "invoice_number": row[14],
                "line_items": line_items,
                "confidence": row[16],
                "extraction_raw": row[17],
                "status": row[18],
                "created_at": str(row[19]) if row[19] else None,
            }


# ── Pydantic models for correction ────────────────────────────────────

class CorrectDocumentRequest(BaseModel):
    """Request body for correcting extracted document fields."""
    vendor: Optional[str] = None
    date: Optional[str] = None  # ISO date string YYYY-MM-DD
    total: Optional[str] = None  # Decimal string
    currency: Optional[str] = None
    subtotal: Optional[str] = None
    tax_gst: Optional[str] = None
    tax_hst: Optional[str] = None
    tax_pst: Optional[str] = None
    tax_other: Optional[str] = None
    payment_method: Optional[str] = None
    invoice_number: Optional[str] = None


@router.patch("/{doc_id}/correct")
async def correct_document(
    doc_id: int,
    req: CorrectDocumentRequest,
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Correct extracted fields on a document.

    Allows users to override LLM extraction results when they spot errors.
    Updates the document record, marks review_status as 'corrected', and
    logs the correction in the audit_log for accountability.
    """
    # Verify document exists and belongs to this user
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, vendor, document_date, total, currency, subtotal,
                          tax_gst, tax_hst, tax_pst, tax_other,
                          payment_method, invoice_number, currency_source
                   FROM documents WHERE id = %s AND user_id = %s""",
                (doc_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document not found")

            old_values = {
                "vendor": row[1],
                "date": str(row[2]) if row[2] else None,
                "total": str(row[3]) if row[3] else None,
                "currency": row[4],
                "subtotal": str(row[5]) if row[5] else None,
                "tax_gst": str(row[6]) if row[6] else None,
                "tax_hst": str(row[7]) if row[7] else None,
                "tax_pst": str(row[8]) if row[8] else None,
                "tax_other": str(row[9]) if row[9] else None,
                "payment_method": row[10],
                "invoice_number": row[11],
                # The audit entry pairs every field the correction writes with
                # what stood there before, and a currency correction writes
                # currency_source too — so it has to be read here or the
                # before-picture raises KeyError and 500s the save.
                "currency_source": row[12],
            }

    # Build SET clauses for provided fields only
    updates: list[str] = []
    params: list = []
    new_values: dict = {}

    if req.vendor is not None:
        updates.append("vendor = %s")
        params.append(req.vendor)
        new_values["vendor"] = req.vendor

    if req.date is not None:
        # Validate date format
        try:
            date.fromisoformat(req.date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format (expected YYYY-MM-DD)")
        updates.append("document_date = %s")
        params.append(req.date)
        new_values["date"] = req.date

    if req.total is not None:
        try:
            Decimal(req.total)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid total (expected decimal string)")
        updates.append("total = %s")
        params.append(req.total)
        new_values["total"] = req.total

    # An empty (or whitespace-only) currency is not a correction: it is the
    # editor saying nothing. Writing it would blank the stored value and, worse,
    # stamp currency_source='stated' on a currency no human ever typed.
    if req.currency is not None and req.currency.strip():
        stated_currency = req.currency.strip()
        updates.append("currency = %s")
        params.append(stated_currency)
        new_values["currency"] = stated_currency
        # A human typed it, so it is stated — and stays stated even where the
        # receipt's tax line would otherwise infer something else
        # (core/document_currency.py).
        updates.append("currency_source = %s")
        params.append("stated")
        new_values["currency_source"] = "stated"

    if req.subtotal is not None:
        updates.append("subtotal = %s")
        params.append(req.subtotal)
        new_values["subtotal"] = req.subtotal

    for tax_field in ["tax_gst", "tax_hst", "tax_pst", "tax_other"]:
        val = getattr(req, tax_field)
        if val is not None:
            updates.append(f"{tax_field} = %s")
            params.append(val)
            new_values[tax_field] = val

    if req.payment_method is not None:
        updates.append("payment_method = %s")
        params.append(req.payment_method)
        new_values["payment_method"] = req.payment_method

    if req.invoice_number is not None:
        updates.append("invoice_number = %s")
        params.append(req.invoice_number)
        new_values["invoice_number"] = req.invoice_number

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to correct")

    # Always mark as corrected
    updates.append("review_status = 'corrected'")

    params.extend([doc_id, db.user_id])

    with db._conn() as conn:
        with conn.cursor() as cur:
            sql = "UPDATE documents SET " + ", ".join(updates) + " WHERE id = %s AND user_id = %s"
            cur.execute(sql, params)

            # Audit log
            cur.execute(
                """INSERT INTO audit_log
                   (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                   VALUES (%s, 'document', %s, 'CORRECT', %s, %s, %s)""",
                (
                    db.user_id,
                    doc_id,
                    # .get, not [k]: a field added to the writer but forgotten
                    # here must cost the audit entry one before-value, never
                    # the user's correction.
                    json.dumps({k: old_values.get(k) for k in new_values}),
                    json.dumps(new_values),
                    user.id,
                ),
            )

    # Send SSE event
    try:
        from server.api.events import send_event
        send_event(user.id, "receipts_updated", {"doc_id": doc_id, "action": "corrected"})
    except Exception:
        pass

    return {
        "status": "ok",
        "doc_id": doc_id,
        "corrected_fields": list(new_values.keys()),
        "review_status": "corrected",
    }


# Project root — server/api/receipts.py is 2 levels below the project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_file_bytes(stored_path: str) -> bytes | None:
    """Download file bytes from the file store, falling back to local disk.

    Handles a stored_path written with backslashes or with an ``uploads/``
    prefix, both of which appear in older rows.
    """
    # Normalize backslashes → forward slashes for the file store
    normalized = stored_path.replace("\\", "/")

    # 1. Try the file store with the normalized path
    try:
        from core.file_storage import download_file as storage_download
        return storage_download(normalized)
    except Exception:
        pass

    # 2. Try the file store without the "uploads/" prefix (a path stored as
    #    uploads/{user_id}/{hash}_{file} against a store keyed {user_id}/...)
    if normalized.startswith("uploads/"):
        stripped = normalized[len("uploads/"):]
        try:
            from core.file_storage import download_file as storage_download
            return storage_download(stripped)
        except Exception:
            pass

    # 3. Fall back to local file on disk
    local_path = _PROJECT_ROOT / normalized
    if local_path.is_file():
        return local_path.read_bytes()

    # 4. Try local path with native separators (Windows backslashes)
    local_native = _PROJECT_ROOT / stored_path
    if local_native.is_file():
        return local_native.read_bytes()

    return None


@router.get("/{doc_id}/preview")
async def preview_receipt(
    doc_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return a PNG thumbnail preview for any document type.

    Converts the first page/content of the document to a PNG image:
    - PDF: renders first page via PyMuPDF
    - Images (jpg, png, gif, bmp, tiff, webp): resizes via Pillow
    - DOCX: extracts text, renders to image via Pillow
    - XLSX/XLS: extracts cell data, renders to image via Pillow
    - CSV: reads text, renders to image via Pillow
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT stored_path, original_filename
                   FROM documents WHERE id = %s AND user_id = %s""",
                (doc_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document not found")

    stored_path = row[0]
    original_filename = row[1] or f"receipt_{doc_id}"

    if not stored_path:
        raise HTTPException(status_code=404, detail="No file path stored")

    file_bytes = _resolve_file_bytes(stored_path)
    if not file_bytes:
        logger.error("File not found in storage or on disk: %s", stored_path)
        raise HTTPException(status_code=404, detail="File not found")

    ext = Path(original_filename).suffix.lower()
    png_bytes = _generate_preview_png(file_bytes, ext)

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _generate_preview_png(file_bytes: bytes, ext: str) -> bytes:
    """Convert document bytes to a PNG preview image."""
    import io

    # PDF → render first page via PyMuPDF
    if ext == ".pdf":
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        if doc.page_count > 0:
            pix = doc[0].get_pixmap(dpi=150)
            png = pix.tobytes("png")
            doc.close()
            return png
        doc.close()
        return _text_to_png("(Empty PDF)")

    # Native images → resize via Pillow
    if ext in (".jpg", ".jpeg", ".jfif", ".png", ".gif", ".bmp",
               ".tiff", ".tif", ".webp"):
        from PIL import Image
        img = Image.open(io.BytesIO(file_bytes))
        if hasattr(img, "n_frames") and img.n_frames > 1:
            img.seek(0)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.thumbnail((800, 800), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # DOCX → extract text, render
    if ext in (".docx", ".doc"):
        try:
            import docx as python_docx
            doc = python_docx.Document(io.BytesIO(file_bytes))
            lines = []
            for p in doc.paragraphs:
                if p.text.strip():
                    lines.append(p.text)
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    lines.append("  |  ".join(cells))
            text = "\n".join(lines[:60]) or "(Empty document)"
            return _text_to_png(text, title=f"DOCX: {Path(ext).stem}")
        except Exception:
            return _text_to_png("(Cannot preview .doc format)")

    # XLSX/XLS → extract cells, render
    if ext in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        lines = []
        ws = wb.active
        if ws:
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 40:
                    lines.append("...")
                    break
                cells = [str(c) if c is not None else "" for c in row]
                lines.append("  |  ".join(cells))
        wb.close()
        text = "\n".join(lines) or "(Empty spreadsheet)"
        return _text_to_png(text)

    # CSV → read text, render
    if ext == ".csv":
        text = file_bytes.decode("utf-8", errors="replace")
        # Limit to first 60 lines
        lines = text.splitlines()[:60]
        text = "\n".join(lines)
        return _text_to_png(text or "(Empty CSV)")

    # Fallback
    return _text_to_png(f"Preview not available for {ext} files")


def _text_to_png(text: str, title: str | None = None, width: int = 700, max_height: int = 900) -> bytes:
    """Render text content to a PNG image using Pillow."""
    import io

    from PIL import Image, ImageDraw, ImageFont

    # Fonts are resolved by name through Pillow, which searches the platform's
    # font directories — so the candidates below are per-platform guesses and
    # every one of them is allowed to be missing. Set NOTE_MONO_FONT /
    # NOTE_SANS_FONT to a font name or an absolute .ttf path to pin them.
    # Pillow's built-in bitmap font is the final fallback and always works.
    def _load_font(env_name: str, candidates: tuple[str, ...], size: int, default=None):
        names = [os.environ[env_name]] if os.environ.get(env_name) else []
        names.extend(candidates)
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except (OSError, IOError):
                continue
        return default if default is not None else ImageFont.load_default()

    font = _load_font(
        "NOTE_MONO_FONT",
        ("DejaVuSansMono.ttf", "consola.ttf", "cour.ttf", "LiberationMono-Regular.ttf",
         "Menlo.ttc", "DejaVuSansMono-Bold.ttf"),
        13,
    )
    title_font = _load_font(
        "NOTE_SANS_FONT",
        ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf", "Helvetica.ttc"),
        15,
        default=font,
    )

    padding = 20
    line_height = 17

    lines = text.splitlines()
    # Truncate very long lines
    lines = [ln[:120] for ln in lines]

    title_lines = 2 if title else 0
    content_height = padding * 2 + (len(lines) + title_lines) * line_height
    height = min(content_height, max_height)

    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    y = padding
    if title:
        draw.text((padding, y), title, fill=(15, 52, 96), font=title_font)
        y += line_height * 2

    for line in lines:
        if y + line_height > height - padding:
            draw.text((padding, y), "...", fill=(108, 117, 125), font=font)
            break
        draw.text((padding, y), line, fill=(30, 30, 30), font=font)
        y += line_height

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/{doc_id}/download")
async def download_receipt(
    doc_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Download a receipt file.

    Tries a signed file-store URL first, then falls back to streaming the file
    from local disk for a path the store does not resolve.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT stored_path, original_filename
                   FROM documents WHERE id = %s AND user_id = %s""",
                (doc_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document not found")

    stored_path = row[0]
    original_filename = row[1] or f"receipt_{doc_id}"

    if not stored_path:
        raise HTTPException(status_code=404, detail="No file path stored for this document")

    # Normalize backslashes for the file store
    normalized = stored_path.replace("\\", "/")

    # 1. Try signed file-store URL
    try:
        from core.file_storage import get_download_url
        url = get_download_url(normalized, original_filename)
        return RedirectResponse(url=url, status_code=302)
    except Exception:
        pass

    # 2. Try the file store without "uploads/" prefix
    if normalized.startswith("uploads/"):
        try:
            from core.file_storage import get_download_url
            url = get_download_url(normalized[len("uploads/"):], original_filename)
            return RedirectResponse(url=url, status_code=302)
        except Exception:
            pass

    # 3. Fall back to local file on disk
    file_bytes = _resolve_file_bytes(stored_path)
    if file_bytes:
        content_type = mimetypes.guess_type(original_filename)[0] or "application/octet-stream"
        safe_name = original_filename.replace('"', "").replace("\\", "").replace("\n", "")
        return Response(
            content=file_bytes,
            media_type=content_type,
            headers={
                "Content-Disposition": f'inline; filename="{safe_name}"',
                "Cache-Control": "private, max-age=3600",
            },
        )

    logger.error("File not found in storage or on disk: %s", stored_path)
    raise HTTPException(status_code=404, detail="File not found")
