"""Transaction listing, filtering, and management API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from config.settings import MAX_UPLOAD_BYTES, MAX_UPLOAD_MB
from core.llm_errors import (
    LOCAL_LLM_UNREACHABLE_CODE,
    LOCAL_LLM_UNREACHABLE_MESSAGE,
    is_local_endpoint_down,
)
from db.database_pg import DatabasePg
from models.transaction import Transaction, account_str
from server.access import require_verified_email
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/transactions", tags=["transactions"])

STATEMENT_EXTENSIONS = {".csv", ".pdf", ".xlsx", ".xls"}
# One cap for every upload route — see config.settings.MAX_UPLOAD_MB.
MAX_STATEMENT_SIZE = MAX_UPLOAD_BYTES

# There is deliberately no ceiling on a statement parse. It runs as a queued
# job, not inside a request, so there is nothing left to race: the per-call
# timeout inside the engine (LOCAL_LLM_TIMEOUT_S) is the only clock, and a job
# that keeps timing out fails on its own after a few waits
# (server/jobs/document_queue.py). A second, job-level deadline would only
# discard a long statement that was parsing correctly.

# Magic-byte signatures that reject garbage before any LLM work.
# CSV has no signature — it is let through and the parser fails on it if it
# is not tabular text. PDF/XLSX/XLS all have stable headers.
_STATEMENT_MAGIC_BYTES: dict[str, list[bytes]] = {
    ".pdf": [b"%PDF"],
    ".xlsx": [b"PK"],  # ZIP-based OOXML
    ".xls": [b"\xd0\xcf\x11\xe0"],  # OLE2 compound doc
}

# The marker a job carries when nobody asked for the statement pipeline — the
# "auto" upload zone sniffed the PDF and chose it (``core/statement_detector``).
# It lives at the top level of the 202 body **and** in the job's ``result``, so
# the queue panel can still offer "no, it's a receipt" after a reload or in a
# second tab, which only ever sees ``GET /api/jobs``.
DETECTED_AS_STATEMENT = "statement"

# In-memory recategorization progress per user. Keyed by user_id.
# Shape: {"status": "running"|"complete"|"error", "total": int, "done": int,
#          "started_at": float, "updated_at": float, "error": str|None}
_recategorize_progress: dict[str, dict] = {}
_recategorize_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers for the LLM-first statement upload flow
# ---------------------------------------------------------------------------


def _decimal_or_none(v):
    """Convert a value to Decimal, or return None for empty/None values."""
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _legacy_account_from_metadata(institution: str, account_type: str, currency: str = "") -> str:
    """Map (institution, account_type, currency) to an account key.

    Returns one of the AccountType values (CAD, USD, Wise, CreditCard, PayPal,
    Amazon) when one fits, otherwise the institution name itself. The
    ``account`` column is plain text, so any value is accepted by the DB layer.
    """
    if institution == "BMO":
        if account_type == "CREDIT_CARD":
            return "CreditCard"
        # Checking: distinguish CAD vs USD by currency field
        if currency == "USD":
            return "USD"
        return "CAD"
    if institution == "Wise":
        return "Wise"
    if institution == "PayPal":
        return "PayPal"
    if institution == "Amazon":
        return "Amazon"
    # Fallback — use institution name; DB column is permissive str
    return institution or "Unknown"


# ---------------------------------------------------------------------------
# Shared import steps — used by the synchronous CSV/XLSX path and by the
# background PDF parse, so both behave identically once rows exist.
# ---------------------------------------------------------------------------


def _store_statement_file(
    db: DatabasePg,
    user_id: str,
    filename: str,
    file_bytes: bytes,
    file_hash: str,
) -> str | None:
    """Put the original upload in the file store and record it as processed.

    Best-effort: a file-store failure must not lose the transactions just
    read out of the document.
    """
    stored_path: str | None = None
    try:
        from core import file_storage
        stored_path = file_storage.upload_file(
            user_id=user_id,
            file_bytes=file_bytes,
            filename=filename,
        )
    except Exception:
        logger.exception("Failed to upload statement to the file store: %s", filename)
        stored_path = None

    # Record the file as processed so re-uploads are caught
    try:
        db.insert_processed_file(file_hash, filename, stored_path=stored_path)
    except Exception:
        pass  # Best-effort — dedup on transactions already handled

    return stored_path


def _insert_parsed_transactions(
    db: DatabasePg,
    transactions_to_insert: list,
    statement_id: str | None,
) -> tuple[int, int, str | None]:
    """Insert rows, counting duplicates. Returns (new, duplicates, account_name)."""
    new_count = 0
    duplicate_count = 0
    detected_account_name: str | None = None

    for txn in transactions_to_insert:
        # Derive a human-readable account name for the response
        if detected_account_name is None:
            detected_account_name = (
                account_str(txn.account)
            ) or txn.institution or "Unknown"
        try:
            db.insert_transaction(
                txn,
                institution=txn.institution,
                account_type=txn.account_type,
                external_id=txn.external_id,
                import_confidence=(
                    txn.import_confidence.value
                    if hasattr(txn.import_confidence, "value")
                    else txn.import_confidence
                ),
                import_flags=list(txn.import_flags) if txn.import_flags else None,
                statement_id=statement_id,
            )
            new_count += 1
        except Exception as exc:
            exc_str = str(exc).lower()
            if (
                "unique" in exc_str
                or "duplicate" in exc_str
                or "idx_transactions_dedup" in exc_str
            ):
                duplicate_count += 1
            else:
                raise

    return new_count, duplicate_count, detected_account_name


def _audit_statement_import(
    db: DatabasePg,
    user_id: str,
    *,
    filename: str,
    new_count: int,
    duplicate_count: int,
    detected_account_name: str | None,
    parse_method: str | None,
    statement_id: str | None,
) -> None:
    try:
        db.insert_audit_log(
            entity_type="transaction",
            entity_id=0,
            action="UPLOAD_STATEMENT",
            performed_by=user_id,
            new_value={
                "filename": filename,
                "imported": new_count,
                "duplicates": duplicate_count,
                "account": detected_account_name,
                "parse_method": parse_method,
                "statement_id": statement_id,
            },
        )
    except Exception:
        pass


def _audit(
    db: DatabasePg,
    user_id: str,
    *,
    entity_type: str,
    entity_id: int,
    action: str,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """Write one concise audit_log row: action, entity, before → after.

    Best-effort by design — the audit trail must never be the reason a user's
    edit or delete fails — but it is written for every action /activity claims
    to record. Category changes, notes and deletions are audited like anything
    else: in a bookkeeping tool, an edit that leaves no trace is worse than a
    missing feature.
    """
    try:
        db.insert_audit_log(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            performed_by=user_id,
            old_value=before,
            new_value=after,
        )
    except Exception:
        logger.warning("audit_log write failed for %s %s", action, entity_id, exc_info=True)


def _current_transaction_field(db: DatabasePg, txn_id: int, column: str) -> str | None:
    """Read one column off a transaction so an audit entry can say "before".

    ``column`` is never user input — call sites pass a literal.
    """
    if column not in {"note", "category", "description", "status"}:
        raise ValueError(f"unsupported audit column {column!r}")
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {column} FROM transactions WHERE id = %s AND user_id = %s",  # noqa: S608
                    (txn_id, db.user_id),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        logger.debug("Could not read %s for audit of txn %s", column, txn_id, exc_info=True)
        return None


def _truncate(text: str | None, limit: int = 120) -> str | None:
    """Shorten free text for an audit entry — the trail is a log, not storage."""
    if text is None:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count_flagged(transactions_to_insert: list) -> int:
    from models.transaction import TransactionStatus as _TS
    return sum(
        1 for t in transactions_to_insert
        if (t.status == _TS.PENDING_REVIEW or t.status == _TS.PENDING_REVIEW.value)
    )


def _send_event(user_id: str, event_type: str, payload: dict) -> None:
    """Push one SSE event, swallowing any failure (the DB row is the truth)."""
    try:
        from server.api.events import send_event
        send_event(user_id, event_type, payload)
    except Exception:
        logger.debug("SSE %s failed for user %s", event_type, user_id[:8], exc_info=True)


# ---------------------------------------------------------------------------
# The queued statement parse
# ---------------------------------------------------------------------------
#
# The parse is a job in ``processing_jobs`` that ``server/jobs/document_queue.py``
# runs, rather than a task the upload endpoint spawns. Three properties follow
# from that: a restart re-queues the work instead of losing it, a folder full of
# statements drains at the configured concurrency instead of all at once, and a
# local LLM outage parks the work rather than failing it.


def _statement_tab_for(
    db: DatabasePg,
    *,
    filename: str,
    imported: int,
    transactions: list,
) -> str | None:
    """Which statement tab the queue's "View" link should open for this upload.

    The transactions page selects a tab by ``transactions.source_file`` (see
    ``web/src/pages/Transactions.tsx``), so a statement that imported at least one
    row of its own **is** a tab, under its own filename.

    A statement that imported none has no tab: every row it carried was already in
    the books under an earlier upload's name. Pointing the View link at a tab that
    does not exist makes the page fall back to whichever tab sorts first — another
    statement's rows, presented as this one's — so point it at the file that
    actually holds those rows instead.

    Returns None when even that cannot be worked out (nothing parsed, rows spanning
    several accounts, an older row with no account). None is the caller's cue to
    link to the statements list instead of to a tab.
    """
    if imported > 0:
        return filename
    if not transactions:
        return None
    try:
        accounts = {
            account_str(txn.account)
            for txn in transactions
            if getattr(txn, "account", None) is not None
        }
        dates = [
            txn.date_posted for txn in transactions
            if getattr(txn, "date_posted", None) is not None
        ]
        if len(accounts) != 1 or not dates:
            # Two accounts in one file, or nothing dated: there is no single tab
            # that is the right answer, so do not guess one.
            return None
        account = next(iter(accounts))
        if not account:
            return None
        found = db.source_file_holding_period(account, min(dates), max(dates))
    except Exception:
        # Worth a WARNING, not a debug line: the only symptom of this failing is
        # a "View" link that quietly goes to the statements list instead of the
        # tab, which nobody would think to report.
        logger.warning(
            "Could not resolve which tab holds %s's rows", filename, exc_info=True
        )
        return None
    return found if isinstance(found, str) and found else None


def _statement_summary(
    *,
    filename: str,
    imported: int,
    duplicates: int,
    replaced: int,
    account: str | None,
    institution: str | None,
    statement_tab: str | None = None,
) -> dict:
    """The ``result.summary`` block a finished statement job carries.

    ``text`` stays ASCII for the reason spelled out on ``_receipt_summary``: a
    machine-composed line also ends up in the server log and in clients that
    decode a charset-less JSON body as latin-1, where a non-ASCII separator
    arrives as mojibake.
    """
    text = f"{imported} row(s) imported"
    if duplicates:
        text += f", {duplicates} duplicate(s)"
    if account:
        text += f" - {account}"
    return {
        "rows": imported,
        "imported": imported,
        "duplicates": duplicates,
        "replaced": replaced,
        "account": account,
        "institution": institution,
        "source_file": filename,
        # Which tab the queue's "View" link opens. Equal to ``source_file`` for a
        # statement that imported rows of its own; the file that holds them when it
        # imported none; None when there is no tab to send anyone to.
        "statement_tab": statement_tab,
        "text": text,
    }


async def run_statement_parse_job(
    *,
    db: DatabasePg,
    user_id: str,
    statement_id: str,
    filename: str,
    file_hash: str,
    source_path: Path,
    replaced_count: int = 0,
    progress_cb=None,
) -> dict:
    """Parse one queued statement and import its rows.

    The ``statements`` row already exists in ``processing`` (the upload endpoint
    inserts it), so everything descriptive is UPDATEd onto it here.

    Contract with the queue:
      * returns the ``result`` to write on the job row on success — and the
        statement row is ``imported`` by then;
      * **raises** ``JobFailure`` on failure and leaves the statement row in
        ``processing``, because the job may still have retries or may be waiting
        for the local LLM endpoint to come back. The queue calls
        ``fail_statement_row`` once it has given up, which is the only place a
        statement is failed.
      * never deletes ``source_path`` — that is the user's stored document, not
        a temp file.

    ``progress_cb(pages_done, pages_total)`` is forwarded to the parser when the
    parser's signature accepts it.
    """
    from core.llm_statement_parser import StatementParseError, parse_statement_with_llm
    from core.pdf_statement_parser import pdf_to_csv
    from models.transaction import AccountType, TransactionStatus
    from server.jobs.document_queue import JobFailure, engine_accepts_progress_cb

    extra: dict = {}
    if progress_cb is not None and engine_accepts_progress_cb(parse_statement_with_llm):
        extra["progress_cb"] = progress_cb

    converted_csv_path: Path | None = None
    try:
        transactions_to_insert: list = []
        statement_metadata = None
        parse_method: str | None = None
        import_confidence: str = "LEGACY"
        import_flags: list[str] = []
        balance_matches: bool | None = None
        balance_difference: str | None = None  # Decimal string or None
        account_override = None

        llm_result = None
        llm_error: str | None = None
        # Set when the failure was the local LLM endpoint refusing the
        # connection — a different answer to the user than "this statement
        # could not be read", and the difference between a job that waits and a
        # job that dies.
        llm_endpoint_down = False
        llm_timed_out = False
        try:
            # No deadline here on purpose: the per-call timeout inside the engine
            # (LOCAL_LLM_TIMEOUT_S) is the only clock a job runs against. The
            # request is long gone, so there is nothing left to race.
            llm_result = await parse_statement_with_llm(
                source_path,
                user_id=user_id,
                source_filename=filename,
                openai_client=None,
                db=db,
                **extra,
            )
        except asyncio.TimeoutError as exc:
            llm_timed_out = True
            llm_error = (
                "The AI service stopped answering part-way through this statement. "
                "It will be retried automatically."
            )
            logger.warning("LLM parse timed out for %s: %s", filename, exc)
        except StatementParseError as exc:
            llm_error = str(exc)
            llm_endpoint_down = is_local_endpoint_down(exc)
            logger.warning("LLM parse failed for %s: %s", filename, exc)
        except Exception as exc:
            if is_local_endpoint_down(exc):
                llm_endpoint_down = True
                llm_error = LOCAL_LLM_UNREACHABLE_MESSAGE
                logger.warning("Local endpoint unreachable for %s: %s", filename, exc)
            else:
                llm_error = f"unexpected: {exc}"
                logger.exception("LLM parse crashed for %s", filename)

        # A dead or silent local LLM endpoint is the queue's problem, not the
        # statement's: raise before the fallback parser gets a turn, so the job
        # waits for the model instead of importing a worse parse or burning an
        # attempt.
        if llm_endpoint_down:
            raise JobFailure(
                error_code=LOCAL_LLM_UNREACHABLE_CODE,
                message=LOCAL_LLM_UNREACHABLE_MESSAGE,
                transient=True,
            )
        if llm_timed_out:
            raise JobFailure(
                error_code="TIMEOUT", message=llm_error or "Parsing timed out.",
                transient=True,
            )

        # Decide: use LLM result, fall back to legacy, or fail.
        # All LLM-parsed rows are imported as UNMATCHED and go straight to
        # reconciliation — no per-row approval step. The statement's
        # confidence tier is still recorded on the row for display.
        use_llm = llm_result is not None
        use_legacy = False
        # A LOW-confidence parse of an institution the regex parser knows how to
        # read is worth a second opinion from that parser; the comparison below
        # then keeps whichever read more rows.
        if use_llm and llm_result.confidence == "LOW":
            institution_guess = llm_result.metadata.institution if llm_result else ""
            if institution_guess in ("BMO", "Wise"):
                use_llm = False
                use_legacy = True

        legacy_rows: list | None = None
        if use_legacy:
            legacy_result = await asyncio.to_thread(pdf_to_csv, source_path)
            if legacy_result is not None:
                converted_csv_path, detected_account = legacy_result
                if detected_account == "CreditCard":
                    account_override = AccountType.CREDIT_CARD
                elif detected_account == "USD":
                    account_override = AccountType.USD
                elif detected_account == "PayPal":
                    account_override = AccountType.PAYPAL
                else:
                    account_override = AccountType.CAD
                from core.bank_parser import parse_csv
                legacy_rows = await asyncio.to_thread(
                    parse_csv, converted_csv_path, account_override=account_override
                )

            # The regex parser is a safety net for a failed LLM parse, not a
            # better parser: its per-institution regexes silently skip any row
            # whose layout they do not anticipate. Never let it discard
            # transactions the LLM did read — a low-confidence-but-complete parse
            # beats a confident partial one.
            if llm_result is not None and (
                legacy_rows is None
                or len(legacy_rows) < len(llm_result.transactions)
            ):
                logger.warning(
                    "%s: legacy parser produced %s row(s) vs the LLM's %d — keeping "
                    "the LLM parse to avoid dropping transactions",
                    filename,
                    "no" if legacy_rows is None else len(legacy_rows),
                    len(llm_result.transactions),
                )
                use_legacy = False
                use_llm = True

        if use_llm:
            # Build Transaction objects from LLM rows
            inst = llm_result.metadata.institution
            acct_type = llm_result.metadata.account_type
            currency_val = llm_result.metadata.currency
            legacy_acct = _legacy_account_from_metadata(inst, acct_type, currency_val)
            for idx, row in enumerate(llm_result.transactions, start=1):
                transactions_to_insert.append(
                    Transaction(
                        account=legacy_acct,
                        transaction_type=row.type.upper(),
                        date_posted=date.fromisoformat(row.date),
                        amount=Decimal(str(row.amount)),
                        currency=currency_val,
                        description=row.description,
                        source_file=filename,
                        source_row=idx,
                        status=TransactionStatus.UNMATCHED,
                        institution=inst,
                        account_type=acct_type,
                        external_id=row.external_id,
                        import_confidence=llm_result.confidence,
                        import_flags=list(llm_result.flags),
                    )
                )
            statement_metadata = llm_result.metadata
            parse_method = llm_result.parse_method
            import_confidence = llm_result.confidence
            import_flags = list(llm_result.flags)
            balance_matches = llm_result.balance_matches
            balance_difference = llm_result.balance_difference  # str

        elif use_legacy:
            transactions_to_insert = legacy_rows or []
            for txn in transactions_to_insert:
                txn.source_file = filename
            parse_method = "legacy_pdf_to_csv"
            import_confidence = "LEGACY"

        else:
            # All paths exhausted, and it was not the endpoint being down (that
            # raised above). This file is genuinely unreadable — a hard failure
            # the queue will retry a couple of times and then give up on.
            raise JobFailure(
                error_code="unable_to_parse",
                message=(
                    "LLM parsing failed and legacy fallback is unavailable. "
                    f"{llm_error or ''}"
                ).strip(),
            )

        # ------------------------------------------------------------------
        # Fill in the statement row (LLM path has metadata; legacy does not)
        # ------------------------------------------------------------------
        if statement_metadata is not None:
            from config.settings import local_llm_config as _lcfg
            from config.settings import openai_config as _ocfg
            from config.settings import statement_parsing_config as _scfg
            _llm_model = None
            if parse_method in ("local_text", "local_vision"):
                _llm_model = _lcfg.model
            elif parse_method in ("gemini_text", "gemini_vision"):
                _llm_model = _scfg.gemini_model
            elif parse_method == "openai_fallback":
                _llm_model = _ocfg.model
            db.update_statement_parsed(
                statement_id,
                user_id,
                institution=statement_metadata.institution,
                account_type=statement_metadata.account_type,
                account_number=statement_metadata.account_number,
                currency=statement_metadata.currency,
                statement_period_start=date.fromisoformat(statement_metadata.statement_period_start),
                statement_period_end=date.fromisoformat(statement_metadata.statement_period_end),
                opening_balance=_decimal_or_none(statement_metadata.opening_balance),
                closing_balance=_decimal_or_none(statement_metadata.closing_balance),
                balance_matches=balance_matches,
                balance_difference=_decimal_or_none(balance_difference),
                page_count=statement_metadata.page_count,
                row_count=len(transactions_to_insert),
                import_confidence=import_confidence,
                import_flags=import_flags,
                parse_method=parse_method,
                llm_model=_llm_model,
                llm_tokens_in=llm_result.llm_tokens_in if llm_result else 0,
                llm_tokens_out=llm_result.llm_tokens_out if llm_result else 0,
                llm_cost_usd=llm_result.llm_cost_usd if llm_result else 0.0,
            )
        else:
            db.update_statement_status(
                statement_id,
                user_id,
                status="imported",
                row_count=len(transactions_to_insert),
                parse_method=parse_method,
            )

        new_count, duplicate_count, detected_account_name = _insert_parsed_transactions(
            db, transactions_to_insert, statement_id
        )

        _audit_statement_import(
            db, user_id,
            filename=filename,
            new_count=new_count,
            duplicate_count=duplicate_count,
            detected_account_name=detected_account_name,
            parse_method=parse_method,
            statement_id=statement_id,
        )

        flagged_row_count = _count_flagged(transactions_to_insert)

        _send_event(
            user_id,
            "transactions_updated",
            {
                "imported": new_count,
                "duplicates": duplicate_count,
                "account": detected_account_name,
                "source_file": filename,
                "replaced": replaced_count,
            },
        )
        complete_payload = {
            "source_file": filename,
            "imported": new_count,
            "duplicates": duplicate_count,
            "batch_confidence": import_confidence,
            "balance_matches": balance_matches,
            "flagged_row_count": flagged_row_count,
            "parse_method": parse_method,
            "statement_id": statement_id,
            "row_count": len(transactions_to_insert),
            "status": "imported",
        }
        # `statement_parsed` is the event the upload queue waits on; the older
        # `statement_parse_complete` name still drives the parsing card on the
        # transactions page, so both go out.
        _send_event(user_id, "statement_parsed", complete_payload)
        _send_event(user_id, "statement_parse_complete", complete_payload)
        logger.info(
            "Statement %s parsed: %d imported, %d duplicate(s), method=%s",
            filename, new_count, duplicate_count, parse_method,
        )
        statement_tab = _statement_tab_for(
            db,
            filename=filename,
            imported=new_count,
            transactions=transactions_to_insert,
        )
        return {
            "statement_id": statement_id,
            "document_id": None,
            "imported": new_count,
            "duplicates": duplicate_count,
            "row_count": len(transactions_to_insert),
            "parse_method": parse_method,
            "batch_confidence": import_confidence,
            "balance_matches": balance_matches,
            "flagged_row_count": flagged_row_count,
            # Top level as well as inside ``summary``: the queue panel's "View"
            # link reads the summary, and ``GET /api/jobs`` callers read the
            # result. Same value, one source.
            "statement_tab": statement_tab,
            "summary": _statement_summary(
                filename=filename,
                imported=new_count,
                duplicates=duplicate_count,
                replaced=replaced_count,
                account=detected_account_name,
                institution=(
                    statement_metadata.institution if statement_metadata else None
                ),
                statement_tab=statement_tab,
            ),
        }

    finally:
        if converted_csv_path:
            try:
                converted_csv_path.unlink(missing_ok=True)
            except Exception:
                pass


# The pair the parsing card listens for. A statement that genuinely could not be
# read gets both; anything that is not a failure gets neither.
STATEMENT_FAILED_EVENTS = ("statement_failed", "statement_parse_error")

# "No, it's a receipt" is a user decision, not a parse failure. It closes the
# statements row the same way, but announcing it as ``statement_failed`` puts a
# red "this statement could not be read" card in front of someone who has just
# said there was nothing to read.
STATEMENT_REROUTED_EVENTS = ("statement_rerouted",)


def close_statement_row(
    db: DatabasePg,
    user_id: str,
    statement_id: str,
    filename: str,
    *,
    error_code: str,
    message: str,
    events: tuple[str, ...] = STATEMENT_FAILED_EVENTS,
    retry_allowed: bool = True,
    extra: dict | None = None,
) -> None:
    """Close out a statements row and tell the browser, in one vocabulary.

    The row is closed identically however this was reached — leaving it in
    ``processing`` is what spins the transactions page forever — but *which*
    events go out is the caller's to say, because "this could not be parsed"
    and "the user re-filed it as a receipt" are different things to show a
    person.
    """
    try:
        db.update_statement_status(
            statement_id, user_id, status="failed", status_message=message,
        )
    except Exception:
        logger.exception(
            "Could not close statement %s — it may be stuck in 'processing' "
            "until the next startup check", statement_id,
        )
    payload = {
        "statement_id": statement_id,
        "source_file": filename,
        "error": message,
        "error_code": error_code,
        "message": message,
        "status": "failed",
        "retry_allowed": retry_allowed,
    }
    if extra:
        payload.update(extra)
    for name in events:
        _send_event(user_id, name, payload)
    logger.warning("Statement %s closed: %s (%s)", filename, message, error_code)


def fail_statement_row(
    db: DatabasePg,
    user_id: str,
    statement_id: str,
    filename: str,
    *,
    error_code: str,
    message: str,
) -> None:
    """Mark a statement failed and tell the browser why.

    Called by the queue once a job has run out of attempts — never while a retry
    is still coming, so the statements row does not flicker between 'failed' and
    'processing' while the worker is still trying.
    """
    close_statement_row(
        db, user_id, statement_id, filename,
        error_code=error_code, message=message, events=STATEMENT_FAILED_EVENTS,
    )


def reroute_statement_row(
    db: DatabasePg,
    user_id: str,
    statement_id: str,
    filename: str,
    *,
    message: str,
) -> None:
    """Close a statements row because the user re-filed the upload as a receipt.

    Emits ``statement_rerouted`` and **not** the failure pair: nothing failed.
    The queue row keeps the user informed from here (``job_updated`` for the new
    receipt job), so there is no retry to offer on the statement either.
    """
    close_statement_row(
        db, user_id, statement_id, filename,
        error_code="REROUTED", message=message,
        events=STATEMENT_REROUTED_EVENTS,
        retry_allowed=False,
        extra={"rerouted_to": "receipt"},
    )


def statement_duplicate_response(
    db: DatabasePg, *, filename: str, file_hash: str, extra: dict | None = None
) -> JSONResponse | None:
    """Answer an upload that has already been accepted.

    Nothing here writes anything, which is what lets it run BEFORE
    ``replace_prior_statement_data``: an identical file dropped twice while the
    first copy is still queued must not delete the in-flight ``statements`` row
    out from under the running job.

    Returns a 202 carrying the job the first upload created when these exact bytes
    are still in the queue, raises 409 when they are already imported, and returns
    None when this is a new upload the caller should go on to process.

    The already-imported check deliberately ignores a row with *this same
    filename*: re-uploading ``statement-march.pdf`` over ``statement-march.pdf`` is the
    documented replace-on-re-upload path (see ``replace_prior_statement_data``),
    not a duplicate. Same bytes under a different name is the duplicate, and is
    what the ``statements_unique_hash`` constraint was always going to refuse.
    """
    if not file_hash:
        return None

    try:
        active_job = db.find_active_processing_job_by_hash("statement", file_hash)
    except Exception:
        logger.debug(
            "in-flight statement duplicate check failed for %s", filename, exc_info=True
        )
        active_job = None
    # A row mapping or None is the contract; anything else means the store could
    # not answer, and an unanswered dedupe check must not reject a real upload.
    if isinstance(active_job, dict):
        from server.jobs.document_queue import job_public

        body = job_public(active_job) or {}
        body["duplicate"] = True
        body["statement_id"] = _job_result_dict(active_job).get("statement_id")
        if extra:
            body.update({k: v for k, v in extra.items() if v is not None})
            if extra.get("reroute_url") is True:
                body["reroute_url"] = f"/api/jobs/{body.get('job_id')}/reroute"
        logger.info(
            "Statement %s (hash=%s) is already queued as job %s - answering with it, "
            "not imported",
            filename, file_hash[:12], str(active_job["id"])[:8],
        )
        return JSONResponse(status_code=202, content=body)

    try:
        existing = db.find_statement_by_hash(file_hash)
    except Exception:
        logger.debug(
            "statement hash duplicate check failed for %s", filename, exc_info=True
        )
        existing = None
    if isinstance(existing, dict) and existing.get("source_file") != filename:
        logger.info(
            "Statement %s (hash=%s) is already imported as '%s' - 409",
            filename, file_hash[:12], existing.get("source_file"),
        )
        raise HTTPException(status_code=409, detail="Statement already imported.")

    return None


def _job_result_dict(job: dict) -> dict:
    """A job row's ``result`` as a dict, whatever the driver handed back."""
    result = job.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return {}
    return dict(result) if isinstance(result, dict) else {}


def replace_prior_statement_data(db: DatabasePg, filename: str) -> int:
    """Clear out whatever an earlier upload of this filename left behind.

    Replace-on-re-upload: if transactions from a file with this name already
    exist, delete them so the new upload cleanly replaces the old data. Without
    this, content-hash dedup would make a filename permanently un-re-uploadable.

    The prior ``statements`` row goes too, even when no transactions were found —
    a failed parse leaves one deliberately, and leaving it in place collides with
    the ``statements_unique_hash`` UNIQUE constraint.

    Returns the number of transactions removed, which is the ``replaced`` count
    the browser shows. Shared with the auto zone in ``server/api/receipts.py``,
    which reaches the statement path by detection rather than by which box the
    file was dropped in and has to do exactly the same clearing.
    """
    replaced_count = db.delete_transactions_by_source_file(filename)
    replaced_statements = db.delete_statements_by_source_file(filename)
    if replaced_count > 0 or replaced_statements > 0:
        db.delete_processed_file_by_filename(filename)
        logger.info(
            "Replacing %d existing transactions and %d statement rows from '%s'",
            replaced_count,
            replaced_statements,
            filename,
        )
    return replaced_count


@router.post("/upload-statement")
async def upload_bank_statement(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Upload a bank statement (CSV or PDF) for transaction import.

    A PDF is parsed by the LLM, which takes far longer than a request should
    wait — so the PDF path stores the file, opens a ``statements`` row in
    ``processing``, queues a job and answers **202** with the job.
    ``server/jobs/document_queue.py`` runs it when its turn comes, at
    ``LOCAL_LLM_MAX_CONCURRENT`` jobs at a time, and a restart re-queues it
    rather than losing it. The browser tracks it over SSE (``job_updated``,
    plus ``statement_parsed`` / ``statement_failed``) or by polling
    ``GET /api/jobs``.

    CSV/XLSX parse deterministically and quickly, so they stay synchronous and
    answer 200 — but with an already-``done`` job object, so the UI has exactly
    one shape to render for both.

    Flow:
    1. Pre-flight: extension + size + magic-byte validation
    2. Duplicate check — the same bytes still in the queue (answer with that job,
       202) or already imported under another name (409). Nothing is deleted:
       a refused upload leaves the books exactly as they were
    3. If same filename existed, delete old txns so the re-upload replaces cleanly
    4. PDF: store the file, insert a 'processing' statement, queue the job, 202.
       CSV/XLSX: parse and import inline, record a finished job, 200.
    """
    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower()

    if ext not in STATEMENT_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(STATEMENT_EXTENSIONS))}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > MAX_STATEMENT_SIZE:
        raise HTTPException(
            status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB}MB)"
        )

    # Magic-byte validation up front — rejects garbage (e.g. a text file
    # renamed `.pdf`) without burning LLM time.
    expected_magic = _STATEMENT_MAGIC_BYTES.get(ext, [])
    if expected_magic and not any(file_bytes[: len(m)] == m for m in expected_magic):
        raise HTTPException(
            status_code=415,
            detail=f"File content does not match '{ext}' format",
        )

    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # Both duplicate checks run before anything is deleted, so a second drop of
    # a file still in the queue cannot delete the in-flight statements row out
    # from under the running job.
    duplicate = statement_duplicate_response(db, filename=filename, file_hash=file_hash)
    if duplicate is not None:
        return duplicate

    replaced_count = replace_prior_statement_data(db, filename)

    if ext == ".pdf":
        return await _queue_statement_pdf(
            db=db,
            user=user,
            filename=filename,
            file_bytes=file_bytes,
            file_hash=file_hash,
            content_type=file.content_type,
            replaced_count=replaced_count,
        )

    # Write to temp file (the CSV/XLSX parsers read from disk). Nothing outlives
    # this request on that path, so a temp file is the right home for it.
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    converted_csv_path = None
    try:
        from core.bank_parser import parse_csv

        account_override = None
        transactions_to_insert: list = []

        if ext in (".xlsx", ".xls"):
            # ── XLSX/XLS branch ───────────────────────────────────────────
            import csv as csv_mod

            import openpyxl

            converted_csv_path = tmp_path.with_suffix(".csv")

            def _xlsx_to_csv(src: Path, dst: Path) -> None:
                wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
                ws = wb.active
                with open(dst, "w", newline="", encoding="utf-8") as cf:
                    writer = csv_mod.writer(cf)
                    for row in ws.iter_rows(values_only=True):
                        writer.writerow(row)
                wb.close()

            # openpyxl load + row iteration is CPU-bound; run off the event loop
            await asyncio.to_thread(_xlsx_to_csv, tmp_path, converted_csv_path)
            parse_path = converted_csv_path
        else:
            # ── CSV branch ────────────────────────────────────────────────
            parse_path = tmp_path

        transactions_to_insert = await asyncio.to_thread(
            parse_csv, parse_path, account_override=account_override
        )
        for txn in transactions_to_insert:
            txn.source_file = filename

        new_count, duplicate_count, detected_account_name = _insert_parsed_transactions(
            db, transactions_to_insert, None
        )

        stored_path = _store_statement_file(db, user.id, filename, file_bytes, file_hash)

        _audit_statement_import(
            db, user.id,
            filename=filename,
            new_count=new_count,
            duplicate_count=duplicate_count,
            detected_account_name=detected_account_name,
            parse_method=None,
            statement_id=None,
        )

        flagged_row_count = _count_flagged(transactions_to_insert)

        _send_event(
            user.id,
            "transactions_updated",
            {
                "imported": new_count,
                "duplicates": duplicate_count,
                "account": detected_account_name,
                "source_file": filename,
                "replaced": replaced_count,
            },
        )

        # A finished job for a parse that never queued. The row costs one INSERT
        # and buys the UI a single shape: every upload, whatever its format,
        # shows up in the same list with the same states.
        statement_tab = _statement_tab_for(
            db,
            filename=filename,
            imported=new_count,
            transactions=transactions_to_insert,
        )
        result = {
            "statement_id": None,
            "document_id": None,
            "imported": new_count,
            "duplicates": duplicate_count,
            "row_count": len(transactions_to_insert),
            "parse_method": None,
            "batch_confidence": "LEGACY",
            "balance_matches": None,
            "flagged_row_count": flagged_row_count,
            "statement_tab": statement_tab,
            "summary": _statement_summary(
                filename=filename,
                imported=new_count,
                duplicates=duplicate_count,
                replaced=replaced_count,
                account=detected_account_name,
                institution=None,
                statement_tab=statement_tab,
            ),
        }
        body: dict = {}
        try:
            from server.jobs.document_queue import emit_job_event, job_public
            job = db.insert_processing_job(
                kind="statement",
                filename=filename,
                stored_path=stored_path,
                file_hash=file_hash,
                status="done",
                result=result,
                finished=True,
            )
            emit_job_event(job)
            body = job_public(job) or {}
        except Exception:
            # The transactions are already imported and the user has been told.
            # A missing queue row is a worse-looking list, not a failed import.
            logger.warning("Could not record a finished job for %s", filename, exc_info=True)

        # Legacy keys stay at the top level: the transactions page reads
        # `imported` / `account` / `replaced` straight off this response.
        body.update({
            "imported": new_count,
            "duplicates": duplicate_count,
            "total": new_count + duplicate_count,
            "account": detected_account_name,
            "source_file": filename,
            "replaced": replaced_count,
            "statement_id": None,
            "batch_confidence": "LEGACY",
            "balance_matches": None,
            "balance_difference": None,
            "flagged_row_count": flagged_row_count,
            "parse_method": None,
        })
        return body

    except HTTPException:
        raise
    except Exception as exc:
        if is_local_endpoint_down(exc):
            logger.warning(
                "Local endpoint unreachable while importing %s: %s", filename, exc
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": LOCAL_LLM_UNREACHABLE_CODE,
                    "message": LOCAL_LLM_UNREACHABLE_MESSAGE,
                    "retry_allowed": True,
                },
            ) from exc
        logger.exception("Failed to process bank statement: %s", filename)
        raise HTTPException(
            status_code=500,
            detail="Failed to parse bank statement",
        )
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        if converted_csv_path:
            try:
                converted_csv_path.unlink(missing_ok=True)
            except Exception:
                pass


async def _queue_statement_pdf(
    *,
    db: DatabasePg,
    user: AuthUser,
    filename: str,
    file_bytes: bytes,
    file_hash: str,
    content_type: str | None,
    replaced_count: int,
    detected_evidence: dict | None = None,
) -> Response:
    """Validate a statement PDF, queue its parse, and answer 202.

    Everything here has to be fast — the whole point is that the browser gets an
    answer well inside a request timeout, however many files were dropped at
    once. The one non-trivial step is the is-this-a-bank-statement text scan,
    which is kept in the request because its verdict is a 400 the user needs
    immediately (and because it would otherwise swallow a receipt uploaded to
    the wrong box without ever saying why).

    ``detected_evidence`` is set only by the "auto" upload zone
    (``POST /api/receipts/upload?auto_detect=1``), which reached this path because
    ``core/statement_detector`` recognised the PDF rather than because the user
    picked the statement box. It does two things: the permissive
    ``is_bank_statement_pdf`` guard is skipped (a *stricter* rule already said yes,
    and re-reading the text layer for a weaker opinion is waste), and the job is
    marked ``detected_as="statement"`` so the browser can offer the one-click
    "no, it's a receipt" reroute instead of making the user upload again.
    """
    from core.pdf_statement_parser import is_bank_statement_pdf

    # The scan and the page count both read the PDF off disk; one temp file
    # serves both and is gone before this function returns. What the worker
    # reads, whenever its turn comes, is the durable copy in the file store.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        if detected_evidence is None:
            try:
                looks_like_statement = await asyncio.to_thread(is_bank_statement_pdf, tmp_path)
            except Exception:
                logger.exception("is_bank_statement_pdf crashed for %s", filename)
                looks_like_statement = False

            if not looks_like_statement:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"'{filename}' does not appear to be a bank statement PDF. "
                        "If it is a receipt, upload it as a receipt instead."
                    ),
                )

        pages_total = await _count_statement_pages(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    # The content-hash dedupe fires here, before any model call, instead of on
    # the INSERT at the end of a successful parse.
    try:
        statement_id = db.insert_statement_processing(
            user_id=user.id,
            source_file=filename,
            source_file_hash=file_hash,
            page_count=pages_total,
        )
    except Exception as exc:
        exc_lower = str(exc).lower()
        if "statements_unique_hash" in exc_lower or (
            "unique" in exc_lower and "statement" in exc_lower
        ):
            raise HTTPException(
                status_code=409, detail="Statement already imported."
            ) from exc
        logger.exception("Could not open a statement row for %s", filename)
        raise HTTPException(
            status_code=500, detail="Failed to parse bank statement"
        ) from exc

    # Store the original now — the worker reads the file from here whenever its
    # turn comes, which may be after a restart, and the user gets to look at what
    # they uploaded even if the parse never succeeds.
    stored_path = _store_statement_file(db, user.id, filename, file_bytes, file_hash)
    if not stored_path:
        db.update_statement_status(
            statement_id, user.id, status="failed",
            status_message="File storage is unavailable, so this upload was not kept. "
                           "Please try again.",
        )
        raise HTTPException(
            status_code=503,
            detail="File storage unavailable. Please try again later.",
        )

    from server.jobs.document_queue import emit_job_event, job_public, wake_worker

    # Seeded, not waited for: the statements row exists from this moment, so
    # the UI can link to it while the job is still queued. The worker reads
    # statement_id and replaced back out of here, and ``mark_done`` merges its
    # own result into this one rather than replacing it, so the detection marker
    # outlives the parse.
    seed: dict = {"statement_id": statement_id, "replaced": replaced_count}
    if detected_evidence is not None:
        seed["detected_as"] = DETECTED_AS_STATEMENT
        seed["detected_evidence"] = detected_evidence

    job = db.insert_processing_job(
        kind="statement",
        filename=filename,
        stored_path=stored_path,
        file_hash=file_hash,
        pages_total=pages_total,
        result=seed,
    )

    _send_event(
        user.id,
        "statement_parse_started",
        {"source_file": filename, "statement_id": statement_id},
    )
    emit_job_event(job)
    wake_worker()

    logger.info(
        "Statement %s accepted as statement %s / job %s at position %s%s",
        filename, statement_id, str(job["id"])[:8], job["position"],
        " (auto-detected)" if detected_evidence is not None else "",
    )

    body = job_public(job) or {}
    body.update({
        # Legacy keys the transactions page already reads off a 202.
        "statement_id": statement_id,
        "source_file": filename,
        "replaced": replaced_count,
        "poll_url": f"/api/statements/{statement_id}",
    })
    if detected_evidence is not None:
        # Top level as well as inside ``result``: the upload caller reads the 202
        # directly, while a reload or a second tab only ever sees ``result``.
        body["detected_as"] = DETECTED_AS_STATEMENT
        body["reroute_url"] = f"/api/jobs/{body.get('job_id')}/reroute"
    return JSONResponse(status_code=202, content=body)


async def _count_statement_pages(path: Path) -> int | None:
    """Page count for the queue's progress display. Best-effort, never raises."""
    def _count() -> int | None:
        try:
            import fitz  # PyMuPDF, already a hard dependency of extraction

            with fitz.open(path) as doc:
                return int(doc.page_count)
        except Exception:
            return None

    try:
        return await asyncio.to_thread(_count)
    except Exception:
        return None


@router.get("/date-range")
async def transaction_date_range(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return the earliest and latest transaction dates for the user."""
    min_date, max_date = db.get_transaction_date_range()
    return {
        "min_date": min_date.isoformat() if min_date else None,
        "max_date": max_date.isoformat() if max_date else None,
    }


class TransactionResponse(BaseModel):
    id: int
    account: str
    transaction_type: str
    date_posted: str
    amount: str
    currency: str
    description: str
    status: str
    has_receipt: bool
    receipt_path: str | None = None
    category: str | None = None
    match_confidence: float | None = None


@router.get("")
async def list_transactions(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    account: str | None = None,
    status: str | None = None,
    month: str | None = None,
    search: str | None = None,
    source_file: str | None = None,
    category: str | None = None,
    start: str | None = None,
    end: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    min_amount: float | None = Query(None, ge=0),
    max_amount: float | None = Query(None, ge=0),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List transactions with filtering, sorting, and pagination.

    Filters:
    - account: CAD, USD, Wise, CreditCard
    - status: UNMATCHED, MATCHED, IGNORED
    - month: YYYY-MM format (e.g., 2026-03)
    - search: exhausts every criterion — exact TXN id (digits, optional '#'),
      substring/regex over the text columns, and an exact dollar-amount match
      when the input looks like money ("$1,234.50", "12.50", "50")
    - source_file: filter by bank statement filename
    - category: exact category name (use "Uncategorized" for NULL)
    - start/end: inclusive ISO date range (YYYY-MM-DD)
    - min_amount/max_amount: absolute-amount range
    - sort_by: column to sort by (date, amount, description, category, status, account)
    - sort_dir: ASC or DESC
    """
    txns, total = db.get_transactions(
        page=page,
        per_page=per_page,
        account=account,
        status=status,
        month=month,
        search=search,
        source_file=source_file,
        category=category,
        start_date=start,
        end_date=end,
        sort_by=sort_by,
        sort_dir=sort_dir,
        min_amount=min_amount,
        max_amount=max_amount,
    )

    # Enrich with receipt info
    doc_paths = db.get_matched_document_paths()
    doc_info = db.get_matched_document_info()
    # Fetch match explanations for matched transactions
    match_explanations: dict[int, str] = {}
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT transaction_id, explanation
                   FROM reconciliation_matches
                   WHERE user_id = %s
                     AND status IN ('AUTO_APPROVED', 'PENDING_REVIEW', 'USER_APPROVED')
                     AND explanation IS NOT NULL""",
                (db.user_id,),
            )
            for row in cur.fetchall():
                match_explanations[row[0]] = row[1]

    try:
        link_info = db.get_linked_transaction_info()
    except Exception:
        link_info = {}

    result = []
    for txn in txns:
        has_receipt = txn.id in doc_paths
        match_infos = doc_info.get(txn.id)  # now list[dict] or None
        primary = match_infos[0] if match_infos else None
        proofs_count = len(match_infos) if match_infos else 0
        # Build full proofs list for multi-proof display
        proofs_list = []
        match_scores: list[float] = []
        if match_infos:
            for mi in match_infos:
                proofs_list.append({
                    "doc_id": str(mi["doc_id"]),
                    "filename": mi["filename"],
                    "match_id": mi["match_id"],
                    "group_id": mi.get("group_id"),
                    "covered_amount": mi.get("covered_amount"),
                    "confidence_score": mi.get("confidence_score"),
                })
                if mi.get("confidence_score") is not None:
                    match_scores.append(float(mi["confidence_score"]))

        linked_list = link_info.get(txn.id, [])
        link_scores = [
            float(link["confidence_score"])
            for link in linked_list
            if link.get("confidence_score") is not None
        ]
        # Confidence = highest score across match + link; None if neither exists.
        all_scores = match_scores + link_scores
        match_confidence = max(all_scores) if all_scores else None

        result.append({
            "id": txn.id,
            "account": account_str(txn.account),
            "transaction_type": txn.transaction_type,
            "date": txn.date_posted.isoformat(),
            "amount": str(txn.amount),
            "currency": txn.currency,
            "description": txn.description,
            "status": txn.status.value,
            "has_receipt": has_receipt,
            "receipt_path": doc_paths.get(txn.id),
            "source_file": txn.source_file,
            "category": getattr(txn, "category", None),
            "note": getattr(txn, "note", None),
            "matched_doc_id": str(primary["doc_id"]) if primary else None,
            "matched_doc_filename": primary["filename"] if primary else None,
            "match_id": primary["match_id"] if primary else None,
            "proofs_count": proofs_count,
            "proofs": proofs_list,
            "linked_transactions": linked_list,
            "match_confidence": match_confidence,
            "match_explanation": match_explanations.get(txn.id),
        })

    return {
        "transactions": result,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


class NoteUpdate(BaseModel):
    note: str | None = None

@router.patch("/{transaction_id}/note")
async def update_transaction_note(
    transaction_id: int,
    body: NoteUpdate,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Update the note on a transaction."""
    note = body.note
    if note is not None:
        note = note.strip() or None
    old_note = _current_transaction_field(db, transaction_id, "note")
    updated = db.update_transaction_note(transaction_id, note)
    if not updated:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if (old_note or None) != note:
        _audit(
            db, user.id,
            entity_type="transaction",
            entity_id=transaction_id,
            action="EDIT_NOTE",
            before=_truncate(old_note),
            after=_truncate(note) or "(note cleared)",
        )
    return {"status": "ok", "transaction_id": transaction_id, "note": note}


@router.patch("/{transaction_id}/toggle-ignore")
async def toggle_transaction_ignore(
    transaction_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Toggle a transaction's IGNORED ("no receipt needed") status.

    IGNORED is a statement about paperwork, never about money: the row is
    self-evident (bank fee, interest, internal transfer) so it needs no receipt
    and no note, but its dollars keep counting in every total, chart, ledger and
    tax figure. Do not confuse it with EXCLUDED, which means the row isn't real.

    If the transaction is currently IGNORED (or EXCLUDED — this is how a user
    restores a row they rejected at import), its real status is re-derived from
    the existing join tables (MATCHED if it has an active receipt match, LINKED
    if it has an active cross-statement link, otherwise UNMATCHED). Otherwise
    it is flipped to IGNORED. Matches, links and notes remain in the database
    untouched, so flipping back restores them.
    """
    from models.transaction import TransactionStatus

    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM transactions WHERE id = %s AND user_id = %s",
                (transaction_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Transaction not found")
            current_status = row[0]

            if current_status in ("IGNORED", "EXCLUDED"):
                # Re-derive by inspecting active matches/links.
                cur.execute(
                    """SELECT
                           EXISTS (
                               SELECT 1 FROM reconciliation_matches
                               WHERE user_id = %s AND transaction_id = %s
                                 AND status != 'USER_REJECTED'
                           ),
                           EXISTS (
                               SELECT 1 FROM transaction_links
                               WHERE user_id = %s
                                 AND status != 'USER_REJECTED'
                                 AND (source_transaction_id = %s
                                      OR target_transaction_id = %s)
                           )""",
                    (db.user_id, transaction_id,
                     db.user_id, transaction_id, transaction_id),
                )
                has_match, has_link = cur.fetchone()
                if has_match:
                    new_status = TransactionStatus.MATCHED.value
                elif has_link:
                    new_status = TransactionStatus.LINKED.value
                else:
                    new_status = TransactionStatus.UNMATCHED.value
            else:
                new_status = TransactionStatus.IGNORED.value

            cur.execute(
                "UPDATE transactions SET status = %s WHERE id = %s AND user_id = %s",
                (new_status, transaction_id, db.user_id),
            )

    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {"transaction_id": transaction_id})
    except Exception:
        logger.debug("Failed to send transactions_updated event", exc_info=True)

    return {
        "status": "ok",
        "transaction_id": transaction_id,
        "new_status": new_status,
    }


class CategoryUpdate(BaseModel):
    category: str | None = None


@router.patch("/{transaction_id}/category")
async def update_transaction_category(
    transaction_id: int,
    body: CategoryUpdate,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Manually set the expense category on a single transaction.

    The category must belong to the user's taxonomy (``user_categories``)
    or ``None``/empty to clear it. When a non-empty category is supplied,
    the row is **hard-locked** (``category_confirmed_by_user=TRUE``) so
    the categorization agent can never overwrite it. Fires a
    ``transactions_updated`` SSE event so every connected client (table,
    detail panel, dashboard cards) refetches.
    """
    category = body.category
    if category is not None:
        category = category.strip() or None

    if category is not None:
        from config.ised_categories import is_assignable
        # Validate against the fixed assignable set (24; excludes the 3 journal-only
        # names). Reject off-list or journal-only names with 422.
        if not is_assignable(category):
            raise HTTPException(
                status_code=422,
                detail=(
                    "category must be one of the fixed ISED categories, or "
                    f"null; '{category}' is not assignable"
                ),
            )
        old_category = _current_transaction_field(db, transaction_id, "category")
        updated = db.confirm_transaction_category(transaction_id, category)
    else:
        old_category = _current_transaction_field(db, transaction_id, "category")
        updated = db.update_transaction_category(transaction_id, None)

    if not updated:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # A recategorization is exactly the "correction" /activity promises to
    # record, and it is what an accountant re-deriving a filing needs to see.
    if (old_category or None) != category:
        _audit(
            db, user.id,
            entity_type="transaction",
            entity_id=transaction_id,
            action="RECATEGORIZE",
            before=old_category or "Uncategorized",
            after=category or "Uncategorized",
        )

    # Notify all connected clients so the table + detail panel + summary refresh.
    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {"transaction_id": transaction_id})
    except Exception:
        logger.debug("Failed to send transactions_updated event", exc_info=True)

    return {"status": "ok", "transaction_id": transaction_id, "category": category}


@router.get("/categories")
async def list_categories(
    user: AuthUser = Depends(get_current_user),
):
    """Return the fixed Canada ISED/GIFI taxonomy for the UI dropdowns.

    Single source of truth is ``config/ised_categories.py`` — this never
    touches ``user_categories``. The response shape is fixed: a flat
    ``categories`` list of the 24 assignable names, ``groups`` with full
    per-option metadata, and ``sections`` order/label metadata.
    """
    from config.ised_categories import api_categories_payload

    return api_categories_payload()


_TAXONOMY_LOCKED_DETAIL = (
    "The category taxonomy is fixed (Canada ISED/GIFI) and cannot be modified."
)


# ── No category CRUD: the taxonomy is a fixed, government-sourced list.
#    The routes stay registered so a stale client gets a 403 that explains
#    itself rather than a bare 404, but every mutation is refused.
@router.post("/categories")
async def create_category(user: AuthUser = Depends(get_current_user)):
    raise HTTPException(status_code=403, detail=_TAXONOMY_LOCKED_DETAIL)


@router.patch("/categories/{name}")
async def rename_category(name: str, user: AuthUser = Depends(get_current_user)):
    raise HTTPException(status_code=403, detail=_TAXONOMY_LOCKED_DETAIL)


@router.delete("/categories/{name}")
async def delete_category(name: str, user: AuthUser = Depends(get_current_user)):
    raise HTTPException(status_code=403, detail=_TAXONOMY_LOCKED_DETAIL)


@router.get("/count")
async def transaction_count(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return total transaction count."""
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = %s",
                (db.user_id,),
            )
            count = cur.fetchone()[0]
            return {"count": count}


@router.get("/source-files")
async def list_source_files(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return distinct source files (bank statements) with transaction counts."""
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT source_file, account, COUNT(*) as txn_count,
                          MIN(date_posted) as min_date, MAX(date_posted) as max_date
                   FROM transactions
                   WHERE user_id = %s
                   GROUP BY source_file, account
                   ORDER BY MAX(created_at) DESC""",
                (db.user_id,),
            )
            rows = cur.fetchall()
            return {
                "source_files": [
                    {
                        "source_file": row[0],
                        "account": row[1],
                        "transaction_count": row[2],
                        "min_date": str(row[3]) if row[3] else None,
                        "max_date": str(row[4]) if row[4] else None,
                    }
                    for row in rows
                ]
            }


_WISE_SOURCE_FILE_RE = re.compile(r"^wise_([A-Z]{3})_(\d{4})(\d{2})$")


def _backfill_wise_statement(
    source_file: str, db: DatabasePg, user_id: str
) -> str | None:
    """Fetch a Wise statement PDF on-demand and store it in the file store.

    Returns the stored_path on success, or None if the backfill is impossible
    (missing credentials, Wise API error, and so on). Used by the
    statement-file endpoint to serve a Wise-synced month whose PDF was never
    archived.
    """
    m = _WISE_SOURCE_FILE_RE.match(source_file)
    if not m:
        return None
    currency, year_s, month_s = m.group(1), m.group(2), m.group(3)
    year, month = int(year_s), int(month_s)

    try:
        import calendar as _calendar
        from datetime import date as _date

        from core.credentials_cloud import CloudCredentialStore
        from core.wise_api import (
            _store_wise_statement_pdf,
            get_borderless_accounts,
            get_profile_id,
        )
    except Exception as e:
        logger.warning("Wise backfill import failed: %s", e)
        return None

    creds = CloudCredentialStore(db, user_id)
    token = creds.retrieve("wise_api_token")
    if not token:
        return None

    try:
        stored_pid_str = creds.retrieve("wise_profile_id")
        stored_pid = int(stored_pid_str) if stored_pid_str else None
        profile_id = get_profile_id(token, preferred_profile_id=stored_pid)
        if not profile_id:
            return None

        accounts = get_borderless_accounts(token, profile_id)
        account_id = next(
            (a.get("id") for a in accounts if a.get("currency") == currency),
            None,
        )
        if not account_id:
            return None

        _, last_day = _calendar.monthrange(year, month)
        start_d = _date(year, month, 1)
        end_d = _date(year, month, last_day)
        today = _date.today()
        if end_d > today:
            end_d = today

        def _log(msg: str) -> None:
            logger.info("[WISE-BACKFILL] %s", msg)

        _store_wise_statement_pdf(
            token=token,
            profile_id=profile_id,
            account_id=account_id,
            currency=currency,
            start_date=start_d,
            end_date=end_d,
            source_file=source_file,
            db=db,
            user_id=user_id,
            log=_log,
        )
    except Exception as e:
        logger.warning("Wise backfill failed for %s: %s", source_file, e)
        return None

    return db.get_processed_file_stored_path(source_file)


@router.get("/statement-file")
async def download_statement_file(
    source_file: str = Query(..., description="Original filename of the bank statement"),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return the original bank statement file (PDF/CSV/XLSX) by source_file.

    Looks up the stored file path in ``processed_files``. When the source_file
    matches a Wise-synced month (``wise_{CUR}_{YYYYMM}``) and no stored_path
    exists, attempts to fetch and archive the statement from the Wise API
    on-demand so the UI can open it.

    Returns a redirect to a signed file-store URL when possible,
    falling back to streaming the file bytes directly.
    """
    import mimetypes

    stored_path = db.get_processed_file_stored_path(source_file)
    if not stored_path:
        stored_path = _backfill_wise_statement(source_file, db, user.id)

    if not stored_path:
        raise HTTPException(
            status_code=404,
            detail="Statement file not available.",
        )

    normalized = stored_path.replace("\\", "/")

    try:
        from core.file_storage import get_download_url
        url = get_download_url(normalized, source_file)
        return RedirectResponse(url=url, status_code=302)
    except Exception:
        pass

    try:
        from core.file_storage import download_file as storage_download
        file_bytes = storage_download(normalized)
    except Exception:
        file_bytes = None

    if file_bytes:
        content_type = mimetypes.guess_type(source_file)[0] or "application/octet-stream"
        safe_name = source_file.replace('"', "").replace("\\", "").replace("\n", "")
        return Response(
            content=file_bytes,
            media_type=content_type,
            headers={
                "Content-Disposition": f'inline; filename="{safe_name}"',
                "Cache-Control": "private, max-age=3600",
            },
        )

    raise HTTPException(status_code=404, detail="Statement file not found in storage")


@router.post("/source-files/clear-all")
async def clear_all_source_files(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Delete every statement and all its transactions for the current user.

    Wipes reconciliation matches, transaction links, ledger entries, transactions,
    and processed_files rows — and resets any matched documents back to EXTRACTED.
    Used by the "Clear All" control in the Transactions UI to reset from scratch.
    """
    uid = user.id
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = %s",
                (uid,),
            )
            total = cur.fetchone()[0]
            if total == 0:
                return {"deleted": 0}

            cur.execute(
                "UPDATE documents SET status = 'EXTRACTED' "
                "WHERE user_id = %s AND status = 'MATCHED'",
                (uid,),
            )
            cur.execute(
                "DELETE FROM ledger_entries WHERE user_id = %s AND match_id IN "
                "(SELECT id FROM reconciliation_matches WHERE user_id = %s)",
                (uid, uid),
            )
            cur.execute(
                "DELETE FROM reconciliation_matches WHERE user_id = %s",
                (uid,),
            )
            cur.execute(
                "DELETE FROM transaction_links WHERE user_id = %s",
                (uid,),
            )
            cur.execute(
                "DELETE FROM transactions WHERE user_id = %s",
                (uid,),
            )
            deleted = cur.rowcount
            # Clear the statements rows too, else their source_file_hash values keep
            # occupying statements_unique_hash and re-uploading any of them 409s.
            cur.execute(
                "DELETE FROM statements WHERE user_id = %s",
                (uid,),
            )
            cur.execute(
                "DELETE FROM processed_files WHERE user_id = %s",
                (uid,),
            )
            conn.commit()

    _audit(
        db, uid,
        entity_type="transaction",
        entity_id=0,
        action="DELETE_STATEMENT",
        before=f"all statements ({deleted} transaction{'' if deleted == 1 else 's'})",
    )

    try:
        from server.api.events import send_event
        send_event(uid, "transactions_updated", {"cleared_all": True})
    except Exception:
        pass

    return {"deleted": deleted}


@router.delete("/source-files/{source_file:path}")
async def delete_source_file(
    source_file: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Delete all transactions from a specific source file (bank statement).

    Also removes associated reconciliation matches and resets matched documents.
    """
    uid = user.id
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Get transaction IDs for this source file
            cur.execute(
                "SELECT id FROM transactions WHERE user_id = %s AND source_file = %s",
                (uid, source_file),
            )
            txn_ids = [row[0] for row in cur.fetchall()]

            # Ledger entries reference reconciliation_matches with no ON DELETE
            # clause, so they must go before the matches or the delete hits the
            # FK RESTRICT.
            cur.execute(
                "DELETE FROM ledger_entries WHERE user_id = %s AND match_id IN "
                "(SELECT id FROM reconciliation_matches WHERE user_id = %s AND transaction_id = ANY(%s))",
                (uid, uid, txn_ids),
            )

            # Remember which receipts were matched to these transactions, drop the
            # matches, then release only the receipts that have no surviving match.
            # A split-payment receipt can prove transactions in two statements;
            # resetting it unconditionally would return it to the unmatched pool while
            # still proving the other statement's charge, and reconciliation could
            # then bind it to a second, unrelated transaction.
            cur.execute(
                """SELECT DISTINCT document_id FROM reconciliation_matches
                   WHERE user_id = %s AND transaction_id = ANY(%s)
                     AND document_id IS NOT NULL""",
                (uid, txn_ids),
            )
            doc_ids = [r[0] for r in cur.fetchall()]

            cur.execute(
                "DELETE FROM reconciliation_matches WHERE user_id = %s AND transaction_id = ANY(%s)",
                (uid, txn_ids),
            )

            if doc_ids:
                cur.execute(
                    """UPDATE documents SET status = 'EXTRACTED', updated_at = NOW()
                       WHERE user_id = %s AND status = 'MATCHED' AND id = ANY(%s)
                         AND NOT EXISTS (
                             SELECT 1 FROM reconciliation_matches m
                             WHERE m.user_id = documents.user_id
                               AND m.document_id = documents.id
                               AND m.status != 'USER_REJECTED'
                         )""",
                    (uid, doc_ids),
                )

            # Delete transaction links referencing these transactions, THEN
            # re-derive the surviving partners' statuses.
            #
            # Judging a partner before its sibling links are gone is wrong once
            # a transaction can hold several: an anchor with two legs in this
            # statement would see a surviving sibling on each pass, stay LINKED,
            # and end up LINKED with nothing linked to it — silently no longer
            # asking for the receipt it still needs.
            cur.execute(
                """SELECT source_transaction_id, target_transaction_id
                   FROM transaction_links
                   WHERE user_id = %s
                     AND (source_transaction_id = ANY(%s)
                          OR target_transaction_id = ANY(%s))""",
                (uid, txn_ids, txn_ids),
            )
            deleted_set = set(txn_ids)
            partners = {
                (tgt_id if src_id in deleted_set else src_id)
                for src_id, tgt_id in cur.fetchall()
            } - deleted_set

            cur.execute(
                """DELETE FROM transaction_links
                   WHERE user_id = %s
                     AND (source_transaction_id = ANY(%s)
                          OR target_transaction_id = ANY(%s))""",
                (uid, txn_ids, txn_ids),
            )

            for partner_id in partners:
                cur.execute(
                    """SELECT
                           (SELECT COUNT(*) FROM transaction_links
                            WHERE user_id = %s
                              AND (source_transaction_id = %s OR target_transaction_id = %s)
                              AND status != 'USER_REJECTED'),
                           (SELECT COUNT(*) FROM reconciliation_matches
                            WHERE user_id = %s AND transaction_id = %s
                              AND status != 'USER_REJECTED')""",
                    (uid, partner_id, partner_id, uid, partner_id),
                )
                remaining_links, remaining_matches = cur.fetchone()
                if remaining_links == 0 and remaining_matches == 0:
                    # Nothing proves this row any more — not a link, not a
                    # receipt. MATCHED is included deliberately: a partner left
                    # MATCHED with no match row is the same inconsistency in the
                    # other table.
                    cur.execute(
                        """UPDATE transactions SET status = 'UNMATCHED'
                           WHERE id = %s AND user_id = %s
                             AND status IN ('LINKED', 'MATCHED')""",
                        (partner_id, uid),
                    )

            # Delete the transactions themselves
            cur.execute(
                "DELETE FROM transactions WHERE user_id = %s AND source_file = %s",
                (uid, source_file),
            )
            deleted = cur.rowcount

            # Drop the statements row too. Leaving it behind would keep its
            # source_file_hash occupying the statements_unique_hash slot, so
            # re-uploading the same file under a different name would 409 as
            # "Statement already imported" even though the user had deleted it.
            cur.execute(
                "DELETE FROM statements WHERE user_id = %s AND source_file = %s",
                (uid, source_file),
            )
            deleted_statements = cur.rowcount

            # Delete processed file entry if exists
            cur.execute(
                "DELETE FROM processed_files WHERE user_id = %s AND original_filename = %s",
                (uid, source_file),
            )
            deleted_processed = cur.rowcount

            # 404 only when the source file left no trace at all. A statement
            # that parsed to zero transactions still has a statements row to
            # clean up, so it is not a 404.
            if not deleted and not deleted_statements and not deleted_processed:
                raise HTTPException(status_code=404, detail="Source file not found")

            conn.commit()

    # A cascading delete of ledger rows has to leave a trace — the count is the
    # part an accountant needs.
    _audit(
        db, uid,
        entity_type="transaction",
        entity_id=0,
        action="DELETE_STATEMENT",
        before=f"{source_file} ({deleted} transaction{'' if deleted == 1 else 's'})",
    )

    try:
        from server.api.events import send_event
        send_event(
            uid,
            "transactions_updated",
            {"deleted": deleted, "source_file": source_file},
        )
    except Exception:
        pass

    return {"deleted": deleted, "source_file": source_file}


@router.get("/{txn_id}/available-matches")
async def available_matches(
    txn_id: int,
    search: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get scored candidate documents for manual matching against a transaction."""
    from datetime import date as date_type
    from decimal import Decimal as D

    from core.reconciliation import score_manual_match

    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, amount, currency, date_posted, description, status
                   FROM transactions WHERE id = %s AND user_id = %s""",
                (txn_id, db.user_id),
            )
            txn_row = cur.fetchone()
            if not txn_row:
                raise HTTPException(status_code=404, detail="Transaction not found")

    txn_amount = D(str(txn_row[1])) if txn_row[1] is not None else D("0")
    txn_currency = txn_row[2] or "CAD"
    txn_date = txn_row[3] if isinstance(txn_row[3], date_type) else date_type.fromisoformat(str(txn_row[3]))
    txn_description = txn_row[4] or ""

    docs = db.get_available_matches_for_transaction(txn_id, search=search, limit=limit * 3)

    candidates = []
    for doc in docs:
        doc_total = D(str(doc["total"])) if doc.get("total") is not None else None
        doc_currency = doc.get("currency")
        doc_date = None
        if doc.get("document_date"):
            dd = doc["document_date"]
            doc_date = dd if isinstance(dd, date_type) else date_type.fromisoformat(str(dd))
        doc_vendor = doc.get("vendor")

        # The row already carries currency_source and the tax columns, so the
        # picker resolves a receipt that states no currency exactly as the
        # matcher does (core/document_currency.py) instead of scoring it 0.
        confidence, amt_score, dt_score, vnd_score = score_manual_match(
            txn_amount, txn_currency, txn_date, txn_description,
            doc_total, doc_currency, doc_date, doc_vendor,
            doc_fields=doc,
            doc_filename=doc.get("original_filename") or "",
        )

        candidates.append({
            "id": doc["id"],
            "filename": doc.get("original_filename") or "",
            "vendor": doc_vendor,
            "date": str(doc_date) if doc_date else None,
            "total": str(doc_total) if doc_total is not None else None,
            "currency": doc_currency,
            "confidence_score": confidence,
            "amount_score": amt_score,
            "date_score": dt_score,
            "vendor_score": vnd_score,
            # Many-to-many split-payment support: surface existing-match info
            # so the UI can offer a "split this receipt across transactions" flow.
            "is_already_matched": bool(doc.get("is_already_matched")),
            "existing_match_count": int(doc.get("existing_match_count") or 0),
            # The user rejected this pair before (or an identical upload of this
            # receipt). Still offered — the picker is where a human may
            # overrule themselves — but labelled, never silently re-suggested.
            "previously_rejected": bool(doc.get("previously_rejected")),
        })

    candidates.sort(key=lambda c: c["confidence_score"], reverse=True)
    candidates = candidates[:limit]

    return {
        "candidates": candidates,
        "transaction": {
            "id": txn_row[0],
            "amount": str(txn_row[1]),
            "currency": txn_currency,
            "date": str(txn_date),
            "description": txn_description,
        },
        "total": len(candidates),
    }


@router.get("/{txn_id}")
async def get_transaction(
    txn_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get full transaction details including matched document info.

    Returns a ``matched_document`` block that the ReceiptSlideOver component
    can render directly, with a ``file_url`` pointing to the receipts download
    endpoint.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.id, t.account, t.transaction_type, t.date_posted, t.amount,
                          t.currency, t.description, t.source_file, t.source_row,
                          t.status, t.created_at, t.category,
                          rm.id as match_id, rm.confidence_score, rm.status as match_status,
                          d.id as doc_id, d.vendor, d.document_date, d.total as doc_total,
                          d.stored_path, d.original_filename, d.currency as doc_currency,
                          d.extraction_confidence,
                          rm.amount_score, rm.date_score, rm.vendor_score, rm.explanation
                   FROM transactions t
                   LEFT JOIN reconciliation_matches rm ON rm.transaction_id = t.id
                       AND rm.status IN ('AUTO_APPROVED', 'USER_APPROVED', 'PENDING_REVIEW')
                   LEFT JOIN documents d ON rm.document_id = d.id
                   WHERE t.id = %s AND t.user_id = %s""",
                (txn_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Transaction not found")

            result = {
                "id": row[0],
                "account": row[1],
                "transaction_type": row[2],
                "date_posted": row[3],
                "date": str(row[3]) if row[3] else None,
                "amount": float(row[4]) if row[4] else 0,
                "currency": row[5],
                "description": row[6],
                "source_file": row[7],
                "source_row": row[8],
                "status": row[9],
                "created_at": str(row[10]) if row[10] else None,
                "category": row[11],
            }

            if row[12]:  # has match (rm.id)
                doc_id = row[15]
                match_confidence = row[13]

                # Build file_url via the receipts download endpoint
                file_url = f"/api/receipts/{doc_id}/download" if doc_id else None

                # Map extraction_confidence string to a numeric value for the UI
                conf_str = row[22] or "medium"
                confidence_map = {"high": 0.95, "medium": 0.75, "low": 0.45}
                confidence_num = confidence_map.get(conf_str, 0.75)

                # Sub-scores and explanation from reconciliation_matches
                amount_score = float(row[23]) if row[23] is not None else None
                date_score = float(row[24]) if row[24] is not None else None
                vendor_score = float(row[25]) if row[25] is not None else None
                explanation = row[26]

                result["match_score"] = match_confidence
                result["bank_reference"] = None
                result["matched_document"] = {
                    "id": str(doc_id),
                    "vendor": row[16] or "Unknown",
                    "date": str(row[17]) if row[17] else "",
                    "total": float(row[18]) if row[18] else 0,
                    "currency": row[21] or row[5],
                    "file_name": row[20] or "",
                    "file_url": file_url,
                    "doc_type": "Receipt",
                    "confidence": confidence_num,
                }
                result["match_explanation"] = explanation
                result["match_sub_scores"] = {
                    "amount_score": amount_score,
                    "date_score": date_score,
                    "vendor_score": vendor_score,
                }

                # Keep legacy format for backwards compat
                result["match"] = {
                    "match_id": row[12],
                    "confidence": match_confidence,
                    "match_status": row[14],
                    "document": {
                        "id": doc_id,
                        "vendor": row[16],
                        "date": row[17],
                        "total": row[18],
                        "stored_path": row[19],
                        "filename": row[20],
                    },
                }
            else:
                result["match_score"] = None
                result["bank_reference"] = None
                result["matched_document"] = None
                result["match_explanation"] = None
                result["match_sub_scores"] = None

            return result


class ReviewAction(BaseModel):
    action: str  # "approve" or "reject"


@router.post("/{txn_id}/review")
async def review_match(
    txn_id: int,
    body: ReviewAction,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Approve or reject a pending match for a transaction."""
    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")

    # Find the pending match for this transaction
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, document_id FROM reconciliation_matches
                   WHERE transaction_id = %s AND user_id = %s AND status = 'PENDING_REVIEW'
                   LIMIT 1""",
                (txn_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="No pending match for this transaction")

            match_id, doc_id = row[0], row[1]

            # reviewed_by carries the user id, as it does at every other site
            # that writes it.
            if body.action == "approve":
                db.update_match_status(match_id, "USER_APPROVED", user.id)
            else:
                db.update_match_status(match_id, "USER_REJECTED", user.id)
                db.update_transaction_status(txn_id, "UNMATCHED")
                db.update_document_status(doc_id, "EXTRACTED")

            return {"status": "ok", "action": body.action, "match_id": match_id}


@router.get("/reviews/pending")
async def pending_reviews(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List all pending review items with transaction and document details."""
    pending = db.get_pending_reviews()

    if not pending:
        return {"reviews": [], "count": 0}

    reviews = []
    with db._conn() as conn:
        with conn.cursor() as cur:
            for match in pending:
                cur.execute(
                    """SELECT t.date_posted, t.amount, t.currency, t.description,
                              d.vendor, d.document_date, d.total, d.stored_path
                       FROM transactions t, documents d
                       WHERE t.id = %s AND t.user_id = %s
                         AND d.id = %s AND d.user_id = %s""",
                    (match.transaction_id, db.user_id, match.document_id, db.user_id),
                )
                row = cur.fetchone()
                if row:
                    reviews.append({
                        "match_id": match.id,
                        "confidence": match.confidence_score,
                        "transaction": {
                            "id": match.transaction_id,
                            "date": row[0],
                            "amount": row[1],
                            "currency": row[2],
                            "description": row[3],
                        },
                        "document": {
                            "id": match.document_id,
                            "vendor": row[4],
                            "date": row[5],
                            "total": row[6],
                            "stored_path": row[7],
                        },
                    })

    return {"reviews": reviews, "count": len(reviews)}


class ApproveAllRequest(BaseModel):
    """Optional body for the bulk approval.

    ``min_confidence`` can only make the endpoint stricter; see
    ``approve_all_reviews``. Out of 0.0–1.0 is a 422.
    """

    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


@router.post("/reviews/approve-all")
async def approve_all_reviews(
    body: ApproveAllRequest | None = None,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Approve every pending review that clears the confidence bar.

    One click settles a batch of matches nobody looked at individually, so it
    may only settle the ones the matcher was already confident about. The floor
    is ``RECONCILIATION_BULK_APPROVE_MIN_CONFIDENCE`` (0.85 by default) and it
    is applied with ``>=``, so a match sitting exactly on the bar is approved.

    ``min_confidence`` in the body may RAISE that bar and never lower it — the
    effective threshold is ``max(setting, min_confidence)``, and a value below
    the setting is ignored rather than rejected. That asymmetry is the point:
    no request can widen a bulk approval past what the setting allows.

    Anything below the bar stays ``PENDING_REVIEW``, writes no audit entry, and
    is counted in ``skipped_below_threshold`` so the caller can say how many
    matches are still waiting for a human.
    """
    from config.settings import reconciliation_config

    min_confidence = float(reconciliation_config.bulk_approve_min_confidence)
    requested = body.min_confidence if body is not None else None
    if requested is not None:
        min_confidence = max(min_confidence, float(requested))

    pending = db.get_pending_reviews()
    approved = 0
    skipped = 0
    with db._conn() as conn:
        with conn.cursor() as cur:
            for match in pending:
                score = match.confidence_score
                if score is None or float(score) < min_confidence:
                    skipped += 1
                    continue
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET status = 'USER_APPROVED',
                           user_action = 'approved',
                           actioned_at = NOW(),
                           reviewed_by = %s,
                           reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (user.id, match.id, db.user_id),
                )
                cur.execute(
                    """INSERT INTO audit_log
                       (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                       VALUES (%s, 'match', %s, 'APPROVE', 'PENDING_REVIEW', 'USER_APPROVED', %s)""",
                    (db.user_id, match.id, user.id),
                )
                approved += 1
    return {
        "approved": approved,
        "skipped_below_threshold": skipped,
        "min_confidence": min_confidence,
    }


# ── Cross-Statement Transaction Linking ────────────────────────────────


VALID_LINK_TYPES = {
    "CREDIT_CARD_PAYMENT", "INTERNAL_TRANSFER", "FX_CONVERSION",
    "WISE_TRANSFER", "REFUND", "OTHER",
}

# Upper bound on one fan-out request. A bulk order refunded item by item is a
# small number of credits; anything past this is a mis-click, and an unbounded
# list would let one request write hundreds of rows.
MAX_LINK_GROUP_SIZE = 20


class CreateLinkRequest(BaseModel):
    source_transaction_id: int
    # One-to-many: an anchor transaction may be linked to several counterparts
    # in a single call (a bulk charge and every credit refunding part of it).
    # The singular field is kept so existing callers keep working.
    target_transaction_id: int | None = None
    target_transaction_ids: list[int] | None = None
    link_type: str
    explanation: str | None = None

    def resolved_targets(self) -> list[int]:
        """Targets from either field, de-duplicated, order preserved."""
        targets = list(self.target_transaction_ids or [])
        if self.target_transaction_id is not None:
            targets.append(self.target_transaction_id)
        return list(dict.fromkeys(targets))


@router.post("/links")
async def create_transaction_link(
    body: CreateLinkRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Link one transaction to one or many counterparts.

    Every target joins a single link group (one shared ``chain_id``), which is
    what makes a split refund representable: one bulk charge linked to each
    credit the merchant issued as it returned items separately.
    """
    if body.link_type not in VALID_LINK_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid link_type. Must be one of: {VALID_LINK_TYPES}",
        )

    targets = body.resolved_targets()
    if not targets:
        raise HTTPException(
            status_code=422,
            detail="Provide target_transaction_id or a non-empty target_transaction_ids",
        )
    if len(targets) > MAX_LINK_GROUP_SIZE:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum {MAX_LINK_GROUP_SIZE} transactions per link group",
        )
    if body.source_transaction_id in targets:
        raise HTTPException(status_code=400, detail="Cannot link a transaction to itself")

    # A transaction may now hold MANY active links, so the only pair-level rule
    # left is that the SAME two transactions can't be linked twice (the DB's
    # idx_transaction_links_dedup). Check up front to name the offending ids
    # instead of surfacing a raw unique-violation.
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT source_transaction_id, target_transaction_id
                   FROM transaction_links
                   WHERE user_id = %s
                     AND status != 'USER_REJECTED'
                     AND ((source_transaction_id = %s AND target_transaction_id = ANY(%s))
                          OR (target_transaction_id = %s AND source_transaction_id = ANY(%s)))""",
                (db.user_id,
                 body.source_transaction_id, targets,
                 body.source_transaction_id, targets),
            )
            already = {
                (row[1] if row[0] == body.source_transaction_id else row[0])
                for row in cur.fetchall()
            }

    if already:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_linked",
                "message": "These transactions are already linked to this one",
                "linked_transaction_ids": sorted(already),
            },
        )

    try:
        result = db.create_transaction_link_group(
            source_txn_id=body.source_transaction_id,
            target_txn_ids=targets,
            link_type=body.link_type,
            confidence_score=1.0,
            explanation=body.explanation,
            match_source="MANUAL",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        msg = str(exc)
        if "cycle" in msg.lower():
            raise HTTPException(status_code=400, detail="Link would create a cycle")
        if "duplicate" in msg.lower() or "unique" in msg.lower():
            raise HTTPException(status_code=409, detail="A link already exists between these transactions")
        raise HTTPException(status_code=500, detail=f"Failed to create link: {msg}")

    # Single-target callers still get the flat shape they were written against.
    if len(result["links"]) == 1:
        result["link_id"] = result["links"][0]["link_id"]
        result["target_transaction_id"] = result["links"][0]["target_transaction_id"]
    return result


@router.get("/{txn_id}/links")
async def get_transaction_links(
    txn_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get all links for a transaction, plus how much of it they offset.

    The ``summary`` block is what turns a list of legs into a readable split
    refund: how much of the charge the credits offset, across how many of them,
    and what is left net.
    """
    try:
        return db.get_transaction_link_summary(txn_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Transaction not found")


@router.get("/{txn_id}/chain")
async def get_transaction_chain(
    txn_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get the full chain of linked transactions starting from this one."""
    nodes = db.get_transaction_chain(txn_id)
    # Find current transaction's index in chain
    current_index = 0
    for i, node in enumerate(nodes):
        if node["transaction_id"] == txn_id:
            current_index = i
            break
    return {"nodes": nodes, "current_index": current_index}


@router.get("/{txn_id}/page")
async def get_transaction_page(
    txn_id: int,
    source_file: str | None = None,
    per_page: int = Query(50, ge=1, le=200),
    account: str | None = None,
    status: str | None = None,
    month: str | None = None,
    search: str | None = None,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get which page a transaction is on given current filters."""
    return db.get_transaction_page(
        transaction_id=txn_id,
        per_page=per_page,
        source_file=source_file,
        account=account,
        status=status,
        month=month,
        search=search,
    )


@router.get("/{txn_id}/proof")
async def get_transaction_proof(
    txn_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get unified proof for a transaction: documents + linked transactions."""
    proofs = db.get_unified_proof(txn_id)
    return {"proofs": proofs}


@router.delete("/links/{link_id}")
async def unlink_transactions(
    link_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Unlink a pair of transactions, and remember the decision.

    The row is stamped USER_REJECTED rather than deleted: a deleted row is a
    forgotten decision, and the deterministic linker — which reconsiders every
    unlinked row on every run — would re-create the very link the user just
    removed. Rejected rows are excluded from the dedup index, so the same pair
    can still be linked again by hand.
    """
    try:
        return db.reject_transaction_link(link_id, reviewed_by=user.id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Link not found")


@router.delete("/link-groups/{chain_id}")
async def unlink_transaction_group(
    chain_id: str,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Unlink every leg of a link group at once, and remember each.

    Unpicking a split refund one credit at a time leaves the charge reading as
    partially refunded at each step; this undoes the whole grouping atomically.
    Same memory rule as the single-leg verb above.
    """
    try:
        return db.reject_transaction_link_group(chain_id, reviewed_by=user.id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Link group not found")
    except Exception as exc:
        # An unparseable uuid reaches psycopg2 as an InvalidTextRepresentation.
        if "uuid" in str(exc).lower():
            raise HTTPException(status_code=422, detail="chain_id must be a UUID")
        raise


class LinkActionRequest(BaseModel):
    action: str  # "approve" or "reject"


@router.patch("/links/{link_id}")
async def update_transaction_link(
    link_id: int,
    body: LinkActionRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Approve or reject a transaction link."""
    action_map = {
        "approve": "USER_APPROVED",
        "reject": "USER_REJECTED",
    }
    new_status = action_map.get(body.action)
    if not new_status:
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")
    try:
        result = db.update_link_status(link_id, new_status, reviewed_by=user.id)
        return result
    except ValueError:
        raise HTTPException(status_code=404, detail="Link not found")


@router.get("/{txn_id}/available-transaction-matches")
async def available_transaction_matches(
    txn_id: int,
    search: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get candidate transactions from other accounts for manual linking.

    Scores candidates by amount match, date proximity, and keyword similarity.
    """
    from core.transaction_linker import (
        _extract_fx_rate,
        _load_keywords,
        _score_account_pair,
        _score_amount,
        _score_date,
        _score_keywords,
    )

    # Get source transaction
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, account, transaction_type, date_posted, amount,
                          currency, description, source_file, source_row,
                          status, created_at, category, note
                   FROM transactions
                   WHERE id = %s AND user_id = %s""",
                (txn_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Transaction not found")

    from datetime import date as date_type
    from decimal import Decimal

    from models.transaction import Transaction, TransactionStatus

    # Transaction.account is a plain `str`: a statement parsed by the LLM can
    # carry any institution name (e.g. "BMO"), which is not a member of
    # the AccountType enum. Coercing through AccountType() would raise
    # ValueError on those rows and 500 the endpoint.
    src_txn = Transaction(
        id=row[0], account=row[1] or "", transaction_type=row[2],
        date_posted=date_type.fromisoformat(str(row[3])) if isinstance(row[3], str) else row[3],
        amount=Decimal(str(row[4])), currency=row[5], description=row[6],
        source_file=row[7], source_row=row[8],
        status=TransactionStatus(row[9]),
        category=row[11], note=row[12],
    )

    # Include all statuses (not just UNMATCHED) so users can link any pair.
    # Don't restrict to opposite transaction type — same-type links are valid
    # (e.g. a provider credit ↔ a bank credit for the same deposit).
    #
    # Scope rules:
    #  * NO search text → suggestion mode: cross-account, most recent rows
    #    (the heuristic transfer-pairing default).
    #  * ANY search text → GLOBAL mode: every transaction in the user's
    #    history is in scope — same account included (refund pairs live in
    #    one account). Matching is substring + regex across every column via
    #    db.txn_search_predicate.
    #
    # Suggestion mode hides only the rows already linked to THIS transaction —
    # the ones that could only produce a duplicate-pair 409. Hiding every row
    # that holds ANY active link would be backwards under one-to-many: after the
    # first refund leg is linked, the remaining legs would vanish from the
    # suggestions for the very charge they belong to.
    #
    # A SEARCH hides nothing. Typing an exact id and getting an empty list is
    # indistinguishable from a broken search — nothing tells the user the row
    # exists and is merely spoken for already. Unavailable rows come back
    # flagged with `unavailable_reason` and render disabled, so the answer is on
    # screen instead of missing.
    # SAFETY: Dynamic WHERE clause is safe — all conditions are hardcoded SQL
    # fragments with %s placeholders. Search text is escaped for LIKE
    # metacharacters and parameterized; the regex arm is parameterized too.
    # No user input is interpolated into the SQL structure.
    has_search = bool((search or "").strip())

    conditions = ["user_id = %s"]
    params: list = [db.user_id]

    if not has_search:
        conditions.append("account != %s")
        params.append(account_str(src_txn.account))
        conditions.append("id != %s")
        params.append(txn_id)
        conditions.append(
            """id NOT IN (
                SELECT CASE WHEN source_transaction_id = %s
                            THEN target_transaction_id
                            ELSE source_transaction_id END
                FROM transaction_links
                WHERE user_id = %s
                  AND status != 'USER_REJECTED'
                  AND (source_transaction_id = %s OR target_transaction_id = %s)
            )"""
        )
        params.extend([txn_id, db.user_id, txn_id, txn_id])
    else:
        predicate, p_params = db.txn_search_predicate(search)
        conditions.append(predicate)
        params.extend(p_params)

    # Counterparts already linked to this transaction — flagged, never hidden
    # from a search.
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT CASE WHEN source_transaction_id = %s
                               THEN target_transaction_id
                               ELSE source_transaction_id END
                   FROM transaction_links
                   WHERE user_id = %s
                     AND status != 'USER_REJECTED'
                     AND (source_transaction_id = %s OR target_transaction_id = %s)""",
                (txn_id, db.user_id, txn_id, txn_id),
            )
            already_linked_ids = {r[0] for r in cur.fetchall()}

    where = " AND ".join(conditions)
    # Suggestions only need recent rows; a search must reach deep history.
    sql_cap = 1000 if has_search else 200

    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, account, transaction_type, date_posted, amount,
                           currency, description, source_file, source_row,
                           status, created_at, category
                    FROM transactions
                    WHERE {where}
                    ORDER BY date_posted DESC
                    LIMIT %s""",
                params + [sql_cap],
            )
            candidate_rows = cur.fetchall()

    keywords = _load_keywords()
    candidates = []

    for crow in candidate_rows:
        tgt_txn = Transaction(
            id=crow[0], account=crow[1] or "", transaction_type=crow[2],
            date_posted=date_type.fromisoformat(str(crow[3])) if isinstance(crow[3], str) else crow[3],
            amount=Decimal(str(crow[4])), currency=crow[5], description=crow[6] or "",
            source_file=crow[7], source_row=crow[8],
            status=TransactionStatus(crow[9]),
            category=crow[11],
        )

        fx_rate = _extract_fx_rate(src_txn.description) or _extract_fx_rate(tgt_txn.description)
        kw_score, inferred_type = _score_keywords(src_txn, tgt_txn, keywords)
        if fx_rate and src_txn.currency != tgt_txn.currency:
            inferred_type = "FX_CONVERSION"
        if account_str(src_txn.account) == account_str(tgt_txn.account):
            if (
                src_txn.currency != tgt_txn.currency
                and src_txn.transaction_type != tgt_txn.transaction_type
            ):
                # Intra-account FX conversion (Wise CAD/USD balances share
                # account "Wise") is a genuine transfer — keep it excluded
                # from income/expense totals via core/transfer_exclusion.py.
                inferred_type = "FX_CONVERSION"
            else:
                # Same-currency same-account pairs are refunds/reversals,
                # never transfers. The dialog submits suggested_link_type
                # unchanged, and transfer-typed links are excluded from
                # income/expense totals — a keyword hit like "WISEMSP" must
                # not silently drop a refund pair from the books.
                inferred_type = (
                    "REFUND" if src_txn.transaction_type != tgt_txn.transaction_type else "OTHER"
                )

        amt_score, _, _ = _score_amount(src_txn, tgt_txn, fx_rate)
        date_score = _score_date(src_txn, tgt_txn, inferred_type)
        acct_score = _score_account_pair(src_txn, tgt_txn, inferred_type)

        composite = 0.45 * amt_score + 0.25 * date_score + 0.20 * kw_score + 0.10 * acct_score

        # Why this row cannot be picked, if it cannot. Returned so a search
        # names the row instead of silently omitting it.
        if tgt_txn.id == txn_id:
            unavailable_reason = "self"
        elif tgt_txn.id in already_linked_ids:
            unavailable_reason = "already_linked"
        else:
            unavailable_reason = None

        candidates.append({
            "id": tgt_txn.id,
            "account": account_str(tgt_txn.account),
            "date": tgt_txn.date_posted.isoformat(),
            "amount": str(tgt_txn.amount),
            "currency": tgt_txn.currency,
            "description": tgt_txn.description,
            "source_file": tgt_txn.source_file,
            "status": tgt_txn.status.value,
            "confidence_score": round(composite, 4),
            "amount_score": round(amt_score, 4),
            "date_score": round(date_score, 4),
            "keyword_score": round(kw_score, 4),
            "account_pair_score": round(acct_score, 4),
            "suggested_link_type": inferred_type,
            "unavailable_reason": unavailable_reason,
        })

    # Selectable rows first, then by score — an unavailable row is context, not
    # a suggestion, and must never outrank something the user can actually pick.
    candidates.sort(
        key=lambda c: (c["unavailable_reason"] is not None, -c["confidence_score"])
    )
    candidates = candidates[:limit]

    return {"candidates": candidates, "total": len(candidates)}


# ------------------------------------------------------------------
# Multi-proof endpoints
# ------------------------------------------------------------------


class AddProofsRequest(BaseModel):
    document_ids: list[int]       # min 1, max 5
    group_id: str | None = None   # optional UUID to bundle under


@router.get("/{txn_id}/proofs")
async def get_transaction_proofs(
    txn_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Get all active proofs for a transaction with coverage statistics."""
    # Verify transaction exists and belongs to user
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, amount, currency FROM transactions WHERE id = %s AND user_id = %s",
                (txn_id, db.user_id),
            )
            txn = cur.fetchone()
            if not txn:
                raise HTTPException(status_code=404, detail="Transaction not found")

    proof_data = db.get_transaction_proofs(txn_id)
    txn_amount = txn[1]
    txn_currency = txn[2] or "CAD"

    # Build response with coverage stats. Coverage only sums proofs in the
    # SAME currency as the transaction — a US$1,000 invoice matched to a
    # C$1,380 charge (a first-class FX-match case) must not read as "72%
    # covered". Cross-currency proofs are counted separately and flagged.
    from decimal import Decimal, InvalidOperation
    proofs = []
    total_proof_amount = Decimal("0")
    cross_currency_count = 0
    doc_count = 0
    link_count = 0

    for p in proof_data["proofs"]:
        amt = None
        proof_currency = (p.get("currency") or txn_currency).upper()
        if p.get("total") is not None:
            try:
                amt = str(p["total"])
                if proof_currency == txn_currency.upper():
                    total_proof_amount += abs(Decimal(amt))
                else:
                    Decimal(amt)  # validate only
                    cross_currency_count += 1
            except (InvalidOperation, TypeError):
                amt = None
        doc_count += 1
        proofs.append({
            "proof_id": p["proof_id"],
            "document_id": p.get("document_id"),
            "proof_type": "document",
            "label": p.get("original_filename", ""),
            "confidence_score": p.get("confidence_score"),
            "amount": amt,
            "currency": p.get("currency"),
            "date": p.get("document_date"),
            "vendor": p.get("vendor"),
            "status": p.get("status", ""),
            "match_source": p.get("match_source", ""),
            "stored_path": p.get("stored_path"),
            "created_at": p.get("created_at"),
        })

    # Compute coverage. When every valued proof is in a different currency
    # the ratio is not computable without FX — report it as unknown
    # (coverage_ratio = None) instead of a falsely low percentage.
    txn_abs = abs(Decimal(str(txn_amount))) if txn_amount else Decimal("0")
    has_same_currency_amounts = total_proof_amount > 0
    if txn_abs and has_same_currency_amounts:
        coverage_ratio = round(float(total_proof_amount / txn_abs), 4)
        is_fully_covered = coverage_ratio >= 0.95
    elif cross_currency_count > 0:
        coverage_ratio = None
        is_fully_covered = None
    else:
        coverage_ratio = 0.0
        is_fully_covered = False

    return {
        "transaction_id": txn_id,
        "proofs": proofs,
        "coverage": {
            "transaction_amount": str(txn_amount) if txn_amount else "0",
            "transaction_currency": txn_currency,
            "total_proof_amount": str(total_proof_amount),
            "coverage_ratio": coverage_ratio,
            "is_fully_covered": is_fully_covered,
            "cross_currency_count": cross_currency_count,
            "proof_count": doc_count + link_count,
            "document_count": doc_count,
            "link_count": link_count,
        },
    }


@router.post("/{txn_id}/proofs", status_code=201)
async def add_proofs(
    txn_id: int,
    body: AddProofsRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Add one or more proof documents to a transaction."""
    if not body.document_ids:
        raise HTTPException(status_code=400, detail="document_ids must not be empty")
    if len(body.document_ids) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 documents per bundle")

    from datetime import date as date_type
    from decimal import Decimal as D

    from core.document_currency import currency_fields
    from core.reconciliation import score_manual_match

    # Verify transaction
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, amount, currency, date_posted, description
                   FROM transactions WHERE id = %s AND user_id = %s""",
                (txn_id, db.user_id),
            )
            txn = cur.fetchone()
            if not txn:
                raise HTTPException(status_code=404, detail="Transaction not found")

    txn_amount = D(str(txn[1])) if txn[1] is not None else D("0")
    txn_currency = txn[2] or "CAD"
    txn_date_raw = txn[3]
    txn_date = txn_date_raw if isinstance(txn_date_raw, date_type) else date_type.fromisoformat(str(txn_date_raw))
    txn_description = txn[4] or ""

    # Resolve group_id
    group_id = body.group_id
    if not group_id and len(body.document_ids) > 1:
        import uuid
        group_id = str(uuid.uuid4())

    # Check for existing proofs — if adding to existing match, use its group_id
    existing_proofs = db.get_transaction_proofs(txn_id)
    if existing_proofs["coverage"]["proof_count"] > 0 and not group_id:
        existing_group_ids = existing_proofs["coverage"]["group_ids"]
        if existing_group_ids:
            group_id = existing_group_ids[0]
        else:
            import uuid
            group_id = str(uuid.uuid4())

    # Adding to an existing match upgrades an existing ONE_TO_ONE to MANY_TO_ONE
    if existing_proofs["coverage"]["proof_count"] > 0:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET match_type = 'MANY_TO_ONE', group_id = %s::uuid
                       WHERE user_id = %s AND transaction_id = %s
                         AND status != 'USER_REJECTED'
                         AND match_type = 'ONE_TO_ONE'""",
                    (group_id, db.user_id, txn_id),
                )

    match_type = "MANY_TO_ONE" if (len(body.document_ids) + existing_proofs["coverage"]["proof_count"]) > 1 else "ONE_TO_ONE"

    added = []
    skipped = []
    claimed_conflicts = []

    # Validate the whole batch BEFORE inserting anything. Inserting as it goes
    # would leave a mixed batch half-persisted when the conflicted documents
    # raise 409, with no SSE refresh to show what landed.
    docs_to_add: list[tuple[int, tuple]] = []
    for doc_id in body.document_ids:
        # Check if already a proof for THIS transaction
        already_proof = any(p["document_id"] == doc_id for p in existing_proofs["proofs"])
        if already_proof:
            skipped.append({"document_id": doc_id, "reason": "already a proof"})
            continue

        # Verify document exists and is not matched to another transaction
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, total, currency, document_date, vendor,"
                    "       currency_source, subtotal, tax_gst, tax_hst,"
                    "       tax_pst, tax_other"
                    " FROM documents WHERE id = %s AND user_id = %s",
                    (doc_id, db.user_id),
                )
                doc = cur.fetchone()
                if not doc:
                    raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

                # Check if document is matched to a DIFFERENT transaction
                cur.execute(
                    """SELECT transaction_id FROM reconciliation_matches
                       WHERE document_id = %s AND user_id = %s
                         AND status != 'USER_REJECTED'
                         AND transaction_id != %s""",
                    (doc_id, db.user_id, txn_id),
                )
                conflict = cur.fetchone()
                if conflict:
                    claimed_conflicts.append(doc_id)
                    continue

        docs_to_add.append((doc_id, doc))

    if claimed_conflicts:
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "claimed_document_ids": claimed_conflicts},
        )

    for doc_id, doc in docs_to_add:
        # Score the match
        doc_total = D(str(doc[2])) if doc[2] is not None else None
        doc_currency = doc[3]
        doc_fields = currency_fields(doc_currency, doc[6:12])
        doc_date = None
        if doc[4]:
            doc_date = doc[4] if isinstance(doc[4], date_type) else date_type.fromisoformat(str(doc[4]))
        doc_vendor = doc[5]

        confidence, amt_score, dt_score, vnd_score = score_manual_match(
            txn_amount, txn_currency, txn_date, txn_description,
            doc_total, doc_currency, doc_date, doc_vendor,
            doc_fields=doc_fields,
        )

        result = db.add_proof_to_transaction(
            txn_id=txn_id,
            doc_id=doc_id,
            match_type=match_type,
            confidence_score=confidence,
            amount_score=amt_score,
            date_score=dt_score,
            vendor_score=vnd_score,
            group_id=group_id,
            covered_amount=str(doc_total) if doc_total is not None else None,
            match_source="MANUAL",
        )
        added.append({
            "match_id": result["proof_id"],
            "document_id": doc_id,
            "confidence_score": round(confidence, 4),
        })
        # Use the resolved group_id for subsequent docs
        group_id = result["group_id"]

    # Emit SSE event
    try:
        from server.api.events import send_event
        send_event(user.id, "proofs_updated", {
            "transaction_id": txn_id,
            "action": "added",
            "proof_count": len(added),
            "group_id": group_id,
        })
    except Exception:
        pass

    return {
        "status": "ok",
        "transaction_id": txn_id,
        "added": added,
        "skipped": skipped,
        "group_id": group_id,
    }


@router.delete("/{txn_id}/proofs/{proof_id}")
async def remove_proof(
    txn_id: int,
    proof_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Remove a single proof from a transaction (soft-delete)."""
    try:
        result = db.remove_proof(proof_id)
    except ValueError as e:
        status_code = 409 if "already removed" in str(e) else 404
        raise HTTPException(status_code=status_code, detail=str(e))

    # Determine transaction status after removal
    txn_status_after = "UNMATCHED" if result["transaction_reverted"] else "MATCHED"

    # Emit SSE event
    try:
        from server.api.events import send_event
        send_event(user.id, "proofs_updated", {
            "transaction_id": txn_id,
            "action": "removed",
            "proof_id": proof_id,
            "document_id": result["document_id"],
        })
    except Exception:
        pass

    return {
        "status": "ok",
        "transaction_id": txn_id,
        "proof_id": proof_id,
        "document_id": result["document_id"],
        "transaction_status_after": txn_status_after,
    }


# ---------------------------------------------------------------------------
# Note attachments — supplementary documents attached to a transaction's note.
# Unlike proofs (reconciliation_matches), these are pure context: they explain
# why a transaction was ignored, what it was for, and so on. They take no part
# in reconciliation, balance coverage or the binder ledger.
# ---------------------------------------------------------------------------

# Permissive but not unbounded — same magic-byte families as receipts plus
# common note-evidence types (txt, csv) without the LLM extraction pipeline.
_NOTE_ATTACHMENT_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic",
    ".txt", ".csv", ".doc", ".docx", ".xls", ".xlsx",
}
_NOTE_ATTACHMENT_MAX_BYTES = MAX_UPLOAD_BYTES  # same cap as every other upload


def _verify_transaction_owned(db: DatabasePg, txn_id: int) -> None:
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM transactions WHERE id = %s AND user_id = %s",
                (txn_id, db.user_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Transaction not found")


@router.get("/{txn_id}/note-attachments")
async def list_note_attachments(
    txn_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """List supplementary documents attached to a transaction's note."""
    _verify_transaction_owned(db, txn_id)
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, filename, mime_type, file_size, created_at
                   FROM note_attachments
                   WHERE user_id = %s AND transaction_id = %s
                   ORDER BY created_at DESC, id DESC""",
                (db.user_id, txn_id),
            )
            rows = cur.fetchall()
    return {
        "transaction_id": txn_id,
        "attachments": [
            {
                "id": r[0],
                "filename": r[1],
                "mime_type": r[2],
                "file_size": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ],
    }


@router.post("/{txn_id}/note-attachments", status_code=201)
async def upload_note_attachment(
    txn_id: int,
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Attach a supplementary document to a transaction's note.

    A note attachment does not trigger LLM extraction, so validation stops at
    extension and size: these are context files for the user's own ledger, not
    inputs to the engine.
    """
    _verify_transaction_owned(db, txn_id)

    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower()
    if ext not in _NOTE_ATTACHMENT_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(_NOTE_ATTACHMENT_EXTS))}",
        )

    file_bytes = await file.read()
    if len(file_bytes) > _NOTE_ATTACHMENT_MAX_BYTES:
        raise HTTPException(
            status_code=413, detail=f"File too large (max {MAX_UPLOAD_MB}MB)"
        )
    if len(file_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # Upload to storage. Reuse the existing receipt storage helper which
    # writes under per-user keys (RLS is enforced at the storage layer).
    from core.file_storage import upload_file as storage_upload
    try:
        storage_key = storage_upload(user.id, file_bytes, filename, file.content_type)
    except Exception as exc:
        logger.error("Storage upload failed for note attachment %s: %s", filename, exc)
        raise HTTPException(
            status_code=503,
            detail="File storage unavailable. Please try again later.",
        )

    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO note_attachments
                   (user_id, transaction_id, filename, stored_path, mime_type, file_size, file_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING id, created_at""",
                (
                    db.user_id, txn_id, filename, storage_key,
                    file.content_type, len(file_bytes), file_hash,
                ),
            )
            row = cur.fetchone()

    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {"transaction_id": txn_id})
    except Exception:
        pass

    return {
        "status": "ok",
        "transaction_id": txn_id,
        "attachment": {
            "id": row[0],
            "filename": filename,
            "mime_type": file.content_type,
            "file_size": len(file_bytes),
            "created_at": row[1].isoformat() if row[1] else None,
        },
    }


@router.get("/{txn_id}/note-attachments/{attachment_id}/download")
async def download_note_attachment(
    txn_id: int,
    attachment_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Download (or redirect to signed URL for) a note attachment."""
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT stored_path, filename, mime_type
                   FROM note_attachments
                   WHERE id = %s AND transaction_id = %s AND user_id = %s""",
                (attachment_id, txn_id, db.user_id),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")

    stored_path, filename, mime_type = row
    try:
        from core.file_storage import get_download_url
        url = get_download_url(stored_path)
        return RedirectResponse(url=url, status_code=307)
    except Exception as exc:
        logger.error("Failed to get download URL for note attachment %s: %s", attachment_id, exc)
        raise HTTPException(status_code=503, detail="Download unavailable")


@router.delete("/{txn_id}/note-attachments/{attachment_id}")
async def delete_note_attachment(
    txn_id: int,
    attachment_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Remove a note attachment. Best-effort storage delete; row is removed."""
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT stored_path FROM note_attachments
                   WHERE id = %s AND transaction_id = %s AND user_id = %s""",
                (attachment_id, txn_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Attachment not found")
            stored_path = row[0]
            cur.execute(
                "DELETE FROM note_attachments WHERE id = %s AND user_id = %s",
                (attachment_id, db.user_id),
            )

    # Best-effort storage delete — orphan files are tolerable; broken UI is not.
    try:
        from core.file_storage import delete_file as storage_delete
        storage_delete(stored_path)
    except Exception:
        logger.debug("Note attachment storage delete failed for %s", stored_path, exc_info=True)

    try:
        from server.api.events import send_event
        send_event(user.id, "transactions_updated", {"transaction_id": txn_id})
    except Exception:
        pass

    return {"status": "ok", "transaction_id": txn_id, "attachment_id": attachment_id}


# ---------------------------------------------------------------------------
# Recategorize All — wipe every transaction's category and rerun the final
# LLM categorization step across the whole account. Used by the "Recategorize"
# button on the Transactions page.
# ---------------------------------------------------------------------------

def _set_recategorize_progress(user_id: str, **fields) -> None:
    with _recategorize_lock:
        existing = _recategorize_progress.get(user_id, {})
        existing.update(fields)
        existing["updated_at"] = time.time()
        _recategorize_progress[user_id] = existing


def _run_recategorize_background(user_id: str, db: DatabasePg) -> None:
    """Clear unconfirmed categories, then run the categorization agent.

    User-confirmed categorizations are preserved by both
    ``db.clear_all_transaction_categories`` (WHERE clause) and the agent's
    SQL-level hard lock in ``update_transaction_categorizations``.
    """
    from core.categorization_agent import (
        RecategorizeProgressReporter,
        run_categorization_agent,
    )
    from server.api.events import send_event

    try:
        _set_recategorize_progress(
            user_id, status="running", total=0, done=0, phase="Starting",
            percent=0, logs=[], summary=None, error=None, started_at=time.time(),
        )

        # 1) Wipe unconfirmed categories + vendor_keys (hard-locked rows are
        #    preserved) and drop the vendor cache so the run cannot reuse a
        #    stale decision from an earlier clustering pass.
        cleared = db.clear_all_transaction_categories()
        cache_cleared = db.clear_vendor_cache()
        # Ensure the fixed taxonomy rows exist (vendor_category_cache FK / persist_run),
        # even for a brand-new user who has never run the agent.
        db.seed_fixed_taxonomy()
        logger.info(
            "Recategorize: cleared %d transaction categories and %d vendor cache rows for user %s",
            cleared, cache_cleared, user_id,
        )
        _set_recategorize_progress(
            user_id, phase="Cleared",
            logs=[
                f"Cleared {cleared} prior categories and {cache_cleared} "
                f"vendor cache rows (hard-locked rows preserved).",
            ],
        )

        try:
            send_event(user_id, "transactions_updated", {"phase": "cleared"})
        except Exception:
            pass

        # 2) Run the full 7-phase categorization agent across the whole account.
        reporter = RecategorizeProgressReporter(
            setter=_set_recategorize_progress, user_id=user_id,
        )
        result = run_categorization_agent(
            db=db, user_id=user_id, scope="all", progress=reporter,
        )

        # 3) Final progress update + SSE.
        _set_recategorize_progress(
            user_id,
            status="complete" if result.status == "complete" else "error",
            phase="Complete" if result.status == "complete" else "Failed",
            percent=100,
            done=result.total,
            total=result.total,
            error=result.error,
            summary=result.as_summary() if hasattr(result, "as_summary") else None,
        )
        try:
            send_event(user_id, "categorization_complete", {
                "total": result.total,
                "categorized": result.total - result.rule_locked if result.total else 0,
                "vendors_categorized": result.vendors_categorized,
                "txns_refined": result.txns_refined,
                "llm_calls": result.llm_calls,
                "status": result.status,
                "error": result.error,
            })
        except Exception:
            pass

    except Exception as exc:
        logger.exception("Recategorize all failed for user %s", user_id)
        _set_recategorize_progress(
            user_id, status="error", phase="Failed", error=str(exc),
        )
        try:
            from server.api.events import send_event
            send_event(user_id, "categorization_complete", {"error": str(exc)})
        except Exception:
            pass


@router.post("/recategorize-all")
async def recategorize_all(
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Wipe every transaction's category and re-run the final categorization
    step of the reconciliation engine across the user's whole account.

    Returns immediately with ``{"status": "started"}``. Callers should listen
    for the ``transactions_updated`` / ``categorization_complete`` SSE events
    or poll ``GET /api/transactions/recategorize-all/progress``.
    """
    uid = user.id

    # Debounce: if a recategorize is already in flight, report it instead of
    # starting another. Allow a new run after 10 minutes of no progress updates
    # in case the previous thread died.
    with _recategorize_lock:
        existing = _recategorize_progress.get(uid)
        if existing and existing.get("status") == "running":
            age = time.time() - existing.get("updated_at", 0)
            if age < 600:
                return {"status": "already_running", "progress": existing}

    _set_recategorize_progress(uid, status="running", total=0, done=0,
                               error=None, started_at=time.time())

    thread = threading.Thread(
        target=_run_recategorize_background,
        args=(uid, db),
        daemon=True,
    )
    thread.start()

    return {"status": "started"}


@router.get("/recategorize-all/progress")
async def recategorize_all_progress(
    user: AuthUser = Depends(get_current_user),
):
    """Return the current recategorization progress for this user."""
    with _recategorize_lock:
        progress = dict(_recategorize_progress.get(user.id, {}))
    if not progress:
        return {"status": "idle", "total": 0, "done": 0}
    return progress
