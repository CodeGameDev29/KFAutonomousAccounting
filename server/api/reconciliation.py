"""Reconciliation API — trigger reconciliation, view status and results."""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import threading
import time
import traceback
from collections import deque
from contextlib import contextmanager
from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config.settings import reconciliation_config
from core.categorization_agent.rule_prepass import (
    _auto_categorize_transactions,
    is_non_reconcilable,
)
from core.spreadsheet_safety import SafeCsvWriter
from db.database_pg import DatabasePg
from models.match import MatchStatus, ReconciliationMatch
from server.access import require_verified_email
from server.auth import AuthUser, get_current_user
from server.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


class _SkipFourthPass(Exception):
    """Internal control-flow signal to skip the global 4th-pass LLM linker
    (used when a reconcile run is scoped to a single statement)."""

# ---------------------------------------------------------------------------
# In-memory reconciliation progress per user
# ---------------------------------------------------------------------------
_reconciliation_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _reconcile_lock_key(user_id: str) -> int:
    """Stable per-user advisory-lock key. Python's hash() is randomized per
    process (PYTHONHASHSEED), so it can't coordinate across instances."""
    digest = hashlib.sha256(f"reconcile:{user_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31)


@contextmanager
def _user_reconcile_lock(db: DatabasePg):
    """Cross-process per-user reconciliation mutex via PG advisory lock.

    Advisory locks are session-bound, so the lock must be taken and released
    on the SAME connection — this holds one pooled connection for the
    duration. Yields True if the lock was acquired, False if another run
    (any process, sync or async) holds it.
    """
    key = _reconcile_lock_key(db.user_id)
    conn = db._pool.getconn()
    acquired = False
    prev_autocommit = conn.autocommit
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = bool(cur.fetchone()[0])
        yield acquired
    finally:
        try:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            try:
                conn.autocommit = prev_autocommit
            except Exception:
                pass
            db._pool.putconn(conn)


def _try_claim_run(uid: str) -> dict | None:
    """Atomically claim the in-process run slot for a user.

    Returns the existing progress dict when a fresh run is already active
    (caller responds already_running); otherwise writes the "Starting..."
    state inside the same lock acquisition — a separate check-then-set had
    a window where two requests could both pass the check.
    """
    with _progress_lock:
        existing = _reconciliation_progress.get(uid)
        if existing and existing.get("status") == "running":
            age = time.time() - existing.get("updated_at", 0)
            if age < 600:  # 10 min staleness guard for died threads
                return existing
        _reconciliation_progress[uid] = {
            "status": "running",
            "phase": "Starting...",
            "detail": "",
            "percent": 0,
            "matches_so_far": 0,
            "updated_at": time.time(),
            "logs": [],
            # The countdown fields start empty and are filled in once a phase
            # that can be counted begins. The panel shows a percentage until
            # then rather than an ETA it cannot yet justify.
            "pairs_total": 0,
            "pairs_done": 0,
            "llm_calls_done": 0,
            "eta_seconds": None,
        }
    return None

# Import from centralized config to avoid drift (env: RECONCILIATION_AUTO_APPROVE_THRESHOLD)
AUTO_APPROVE_THRESHOLD = reconciliation_config.auto_approve_threshold

class ReconcileRequest(BaseModel):
    auto_approve_threshold: float | None = None
    # Optional scope window. When provided, candidate transactions + documents
    # are filtered to [start_date, end_date] (inclusive, ISO YYYY-MM-DD).
    # Both omitted = legacy behaviour (all unmatched rows in scope).
    start_date: str | None = None
    end_date: str | None = None

# Default maximum date variance (days) between a proof of transaction and a bank
# transaction. Comes from ReconciliationConfig.date_window_days
# (env: RECONCILIATION_DATE_WINDOW_DAYS) so the engine, the settings endpoint and
# /health all quote one number. An account overrides it for itself via
# /api/reconciliation/settings.
DEFAULT_MAX_DATE_DAYS = reconciliation_config.date_window_days

# Valid options for the dropdown
MAX_DATE_DAYS_OPTIONS = [7, 14, 21, 30, 45, 60, 90]


def _get_max_date_days(db: DatabasePg) -> int:
    """This account's max_date_variance_days, or the configured default."""
    fallback = int(reconciliation_config.date_window_days)
    try:
        profile = db.get_business_profile()
        if profile and profile.get("config_json"):
            return int(profile["config_json"].get("max_date_variance_days", fallback))
    except Exception:
        pass
    return fallback

# ``_CATEGORY_PATTERNS``, ``NON_RECONCILABLE_CATEGORIES`` and
# ``_auto_categorize_transactions`` are imported at the top of this module
# from ``core.categorization_agent.rule_prepass``.


def _generate_match_explanation(
    match: "ReconciliationMatch",
    txn_map: dict,
    doc_map: dict,
) -> str | None:
    """Generate a human-readable explanation for a reconciliation match.

    Uses the full ``core.match_explainer.explain_match`` module when the
    transaction and document are available in the lookup maps.  Falls back
    to a simple confidence-based summary otherwise.
    """

    try:
        txn = txn_map.get(match.transaction_id)
        doc = doc_map.get(match.document_id)

        if txn and doc:
            from core.document_currency import resolve_document_currency
            from core.match_explainer import explain_match

            # The currency the receipt is read in, and how that was decided — a receipt
            # that never named one is read in the bank line's currency, not
            # called cross-currency against a blank column.
            doc_code, doc_currency_source = resolve_document_currency(doc)

            explanation = explain_match(
                txn_amount=txn.amount,
                txn_currency=txn.currency,
                txn_date=txn.date_posted.isoformat() if txn.date_posted else "",
                txn_description=txn.description,
                doc_total=doc.total or 0,
                doc_currency=doc_code or "",
                doc_date=doc.document_date.isoformat() if doc.document_date else None,
                doc_vendor=doc.vendor,
                confidence_score=match.confidence_score,
                amount_score=match.amount_score,
                date_score=match.date_score,
                vendor_score=match.vendor_score,
                match_type=match.match_type.value,
                doc_currency_source=doc_currency_source,
            )
            return explanation.summary
        else:
            # Fallback: simple confidence-based summary
            confidence = match.confidence_score
            if confidence > 0.90:
                strength = "Strong match"
            elif confidence > 0.70:
                strength = "Good match"
            elif confidence > 0.50:
                strength = "Partial match"
            else:
                strength = "Weak match"

            parts = [f"{strength}: {confidence:.0%} confidence"]
            if match.amount_score > 0.95:
                parts.append("exact amount match")
            if match.date_score > 0.90:
                parts.append("close date")
            if match.vendor_score > 0.70:
                parts.append("vendor name match")
            return ". ".join(parts) + "."
    except Exception:
        logger.debug("Failed to generate match explanation", exc_info=True)
        return None


def _settled_currency_phrase(doc) -> str:
    """The phrase "the same currency", qualified when the receipt printed none.

    ``core.reconciliation._unambiguous_candidate`` settles a pair whose
    currency was *inferred* from a Canadian tax line exactly as readily as one
    the receipt stated — only an ``assumed`` currency is refused outright, and
    that gate is deliberate and unchanged. The sentence it produces must then
    not tell a user the two amounts were "in the same currency" flatly, as
    though the receipt had said so: for a receipt that prints a bare "$", the
    currency is the engine's reading of a Canadian tax line, and a user checking
    a tax filing is entitled to know which of the two it was.
    """
    try:
        from core.document_currency import INFERRED, resolve_document_currency

        _, source = resolve_document_currency(doc)
        if source == INFERRED:
            return "the same currency (inferred from the receipt's Canadian tax line)"
    except Exception:
        logger.debug("Could not resolve the document currency", exc_info=True)
    return "the same currency"


def _load_rejected_pairs(db: DatabasePg) -> set[tuple[int, int]]:
    """The user's rejected (transaction, document) pairs, or an empty set.

    A read failure must not turn into a re-proposal storm, but it also must not
    fail a whole reconciliation run — it is logged and the run proceeds with
    the other gate (``_store_matches``) still reading it.
    """
    try:
        return db.get_user_rejected_pairs()
    except Exception:
        logger.exception(
            "Could not read the user's rejected pairs; this run may re-propose one"
        )
        return set()


def _store_matches(
    db: DatabasePg,
    matches: list,
    txn_map: dict,
    doc_map: dict,
    auto_approve_threshold: float,
    rejected_pairs: "set[tuple[int, int]] | None" = None,
) -> dict:
    """Persist a run's matches under the confidence floor and displacement rule.

    Not every pair the matcher proposes may be written through as MATCHED. A
    weak pair marked MATCHED *looks* reconciled to the user, counts toward the
    readiness figure, and — being MATCHED — leaves every later candidate pool,
    so the receipt that would have matched it perfectly can never be found.

    The rule, with the thresholds named in ``core.reconciliation``:

    * **Settled** (confidence at or above ``MATCH_CONFIDENCE_FLOOR``, and no
      vendor contradiction): stored as before — AUTO_APPROVED above the
      auto-approve threshold, else PENDING_REVIEW — and the transaction and
      receipt are marked MATCHED.
    * **Flagged** (below the floor, or a zero vendor score against a
      description that plainly names somebody else): stored as PENDING_REVIEW
      with the reason in front of the explanation, and the transaction and
      receipt are **left unmatched**, so they stay in the candidate pool and a
      later run can do better.
    * **Displacement**: a new pair takes a transaction (or a receipt) off an
      existing match only when that match is itself flagged — below the floor,
      still PENDING_REVIEW, and automatic — and the new one beats it by at
      least ``DISPLACEMENT_MARGIN``. The displaced row is kept for the audit
      trail and its receipt returns to unmatched. A human's decision (a manual
      match, or one already approved) is never displaced by a machine.
    * **User-rejected**: a pair the user has rejected is never written again,
      whatever this run scored it. The engine already drops those pairs at
      candidate generation; this is the second gate, because the dedup index
      (``idx_matches_dedup``) excludes USER_REJECTED rows, so an insert of a
      rejected pair would otherwise succeed at the database level and recreate
      the pair the user threw out.

    Returns a count dict for the run summary.
    """
    from core.reconciliation import (
        DISPLACEMENT_MARGIN,
        MATCH_CONFIDENCE_FLOOR,
        classify_match,
    )

    counts = {
        "stored": 0, "auto_approved": 0, "pending_review": 0,
        "flagged": 0, "displaced": 0, "not_stored": 0, "refreshed": 0,
        "user_rejected_skipped": 0,
    }
    if not matches:
        return counts

    if rejected_pairs is None:
        reader = getattr(db, "get_user_rejected_pairs", None)
        rejected_pairs = reader() if callable(reader) else set()

    txn_ids = [m.transaction_id for m in matches]
    doc_ids = [m.document_id for m in matches]
    try:
        existing_rows = db.get_active_matches_for(txn_ids, doc_ids)
    except Exception:
        logger.exception("Could not read existing matches; storing without displacement")
        existing_rows = []

    by_txn: dict[int, list[dict]] = {}
    by_doc: dict[int, list[dict]] = {}
    for row in existing_rows:
        by_txn.setdefault(row["transaction_id"], []).append(row)
        by_doc.setdefault(row["document_id"], []).append(row)

    def _displaceable(row: dict, challenger_confidence: float) -> bool:
        return (
            row.get("status") == "PENDING_REVIEW"
            and (row.get("match_source") or "AUTO") != "MANUAL"
            and row.get("confidence_score", 0.0) < MATCH_CONFIDENCE_FLOOR
            and challenger_confidence
            >= row.get("confidence_score", 0.0) + DISPLACEMENT_MARGIN
        )

    # Best first, so the strongest pair gets first refusal on a contested row.
    for match in sorted(matches, key=lambda m: m.confidence_score, reverse=True):
        if (match.transaction_id, match.document_id) in rejected_pairs:
            counts["user_rejected_skipped"] += 1
            logger.info(
                "Match txn=%s doc=%s not stored: the user rejected this pair",
                match.transaction_id, match.document_id,
            )
            continue

        txn = txn_map.get(match.transaction_id)
        doc = doc_map.get(match.document_id)

        classification = classify_match(
            match.confidence_score,
            match.vendor_score,
            (getattr(txn, "description", "") or ""),
            getattr(doc, "vendor", None),
            (getattr(doc, "original_filename", "") or ""),
            amount_score=match.amount_score,
        )

        explanation = _generate_match_explanation(match, txn_map, doc_map)
        # Say plainly how the pair was decided. A user looking at a match in the
        # Review panel should not have to guess whether a model was involved.
        adjudication = getattr(match, "adjudication", "agents")
        if adjudication == "deterministic":
            explanation = (
                f"{explanation or ''} Settled deterministically (adjudication: "
                f"deterministic) \u2014 one candidate, the same amount in "
                f"{_settled_currency_phrase(doc)}, dates within days, and the "
                f"bank line names this vendor. No AI agent was asked."
            ).strip()
        if classification.flagged:
            explanation = (
                f"Flagged for review \u2014 {classification.reason_text()}. "
                f"{explanation or ''}"
            ).strip()

        if classification.flagged:
            match.status = MatchStatus("PENDING_REVIEW")
        elif match.confidence_score > auto_approve_threshold:
            match.status = MatchStatus("AUTO_APPROVED")
        else:
            match.status = MatchStatus("PENDING_REVIEW")
        match.explanation = explanation

        # Already proposed this exact pair? Re-score it rather than adding a twin.
        same_pair = next(
            (r for r in by_txn.get(match.transaction_id, [])
             if r["document_id"] == match.document_id),
            None,
        )
        if same_pair is not None:
            if same_pair["status"] == "PENDING_REVIEW":
                db.refresh_match_scores(
                    same_pair["id"], match.confidence_score, match.amount_score,
                    match.date_score, match.vendor_score, explanation,
                    match.status.value,
                )
                same_pair["confidence_score"] = match.confidence_score
                same_pair["status"] = match.status.value
                counts["refreshed"] += 1
                if classification.flagged:
                    counts["flagged"] += 1
                else:
                    _settle(db, match)
            else:
                counts["not_stored"] += 1
            continue

        competitors = {
            r["id"]: r
            for r in by_txn.get(match.transaction_id, []) + by_doc.get(match.document_id, [])
        }
        blockers = [
            r for r in competitors.values()
            if not _displaceable(r, match.confidence_score)
        ]
        if blockers:
            counts["not_stored"] += 1
            logger.info(
                "Match txn=%s doc=%s (%.2f) not stored: %d existing match(es) hold it",
                match.transaction_id, match.document_id, match.confidence_score,
                len(blockers),
            )
            continue

        for row in competitors.values():
            reason = (
                f"Superseded by a stronger match on transaction "
                f"{match.transaction_id} ({row['confidence_score']:.0%} \u2192 "
                f"{match.confidence_score:.0%}, margin {DISPLACEMENT_MARGIN:.0%}); "
                f"this proof of transaction is unmatched again."
            )
            try:
                db.supersede_match(row["id"], reason)
                counts["displaced"] += 1
                row["status"] = "USER_REJECTED"
                logger.info(
                    "Superseded match %s (%.2f) with txn=%s doc=%s (%.2f)",
                    row["id"], row["confidence_score"], match.transaction_id,
                    match.document_id, match.confidence_score,
                )
            except Exception:
                logger.exception("Could not supersede match %s", row["id"])
                counts["not_stored"] += 1
                break
        else:
            new_id = db.insert_match(match)
            counts["stored"] += 1
            if classification.flagged:
                counts["flagged"] += 1
                logger.info(
                    "Match txn=%s doc=%s FLAGGED for review (%.2f): %s",
                    match.transaction_id, match.document_id,
                    match.confidence_score, classification.reason_text(),
                )
            else:
                _settle(db, match)
            if match.status.value == "AUTO_APPROVED":
                counts["auto_approved"] += 1
            else:
                counts["pending_review"] += 1
            stored_row = {
                "id": new_id,
                "transaction_id": match.transaction_id,
                "document_id": match.document_id,
                "confidence_score": match.confidence_score,
                "status": match.status.value,
                "match_source": "AUTO",
                "match_type": match.match_type.value,
            }
            by_txn.setdefault(match.transaction_id, []).append(stored_row)
            by_doc.setdefault(match.document_id, []).append(stored_row)

    return counts


def _settle(db: DatabasePg, match) -> None:
    """Mark a settled pair as reconciled on both sides."""
    db.update_transaction_status(match.transaction_id, "MATCHED")
    db.update_document_status(match.document_id, "MATCHED")


async def _run_reconciliation_internal(
    user: AuthUser, db: DatabasePg, auto_approve_threshold: float | None = None,
    start_date: str | None = None, end_date: str | None = None,
) -> dict:
    """Shared reconciliation logic used by both /reconciliation/run and /actions/sync.

    When `start_date` / `end_date` (ISO YYYY-MM-DD) are supplied the candidate
    pool is scoped to that window. Without a scope, UNMATCHED rows from earlier
    periods stay in the pool and can crowd a single-month run with look-alikes,
    costing matches the scoped run would have made.
    """
    from core.reconciliation import find_matches_multi_pass

    unmatched_txns = db.get_unmatched_transactions(since=start_date, until=end_date)
    unmatched_docs = db.get_unmatched_documents(since=start_date, until=end_date)

    categories = _auto_categorize_transactions(unmatched_txns)  # canonical labels for the histogram

    # ── Run cross-statement transaction linking ──
    #
    # Runs BEFORE the non-reconcilable stamping, over the whole book rather than
    # this run's scope. Both are deliberate: "Credit card payment" is itself a
    # non-reconcilable rule, so stamping first would mark a leg IGNORED and take
    # it out of the pool before its counterpart on the other statement was ever
    # looked at — and a transfer's two legs live on two different statements by
    # construction. See DatabasePg.get_linkable_transactions.
    linked_txn_ids: set[int] = set()
    try:
        from core.transaction_linker import run_transaction_linking
        linker_result = run_transaction_linking(
            transactions=db.get_linkable_transactions(),
            rejected_pairs=db.get_user_rejected_link_pairs(),
        )
        for link in linker_result.links:
            try:
                db.create_transaction_link(
                    source_txn_id=link.source_transaction_id,
                    target_txn_id=link.target_transaction_id,
                    link_type=link.link_type,
                    confidence_score=link.confidence_score,
                    match_source="AUTO",
                    explanation=link.explanation,
                    exchange_rate=link.exchange_rate,
                    amount_variance=link.amount_variance,
                    # Legs of one refund fan-out share a chain_id — without
                    # passing it through, each leg lands in its own singleton
                    # group and the grouping is lost at the DB boundary.
                    chain_id=link.chain_id,
                    chain_position=link.chain_position,
                )
                linked_txn_ids.add(link.source_transaction_id)
                linked_txn_ids.add(link.target_transaction_id)
            except Exception as link_exc:
                logger.warning("Failed to create link %s->%s: %s",
                               link.source_transaction_id, link.target_transaction_id, link_exc)
    except Exception as exc:
        logger.warning("Transaction linking failed (continuing with reconciliation): %s", exc)

    # Identify non-reconcilable transactions. A row the linker just explained is
    # skipped: it is LINKED now, and stamping it IGNORED would overwrite a real
    # explanation with "no receipt needed" and hide the link in the grid.
    non_reconcilable_ids = set()
    non_reconcilable_cats: dict[str, int] = {}
    for txn in unmatched_txns:
        # Gate on the description (canonical names are shared across reconcilable
        # and non-reconcilable flows, so a name-membership test no longer works).
        if txn.id not in linked_txn_ids and is_non_reconcilable(txn.description):
            cat = categories.get(txn.id, "Uncategorized")
            non_reconcilable_ids.add(txn.id)
            non_reconcilable_cats[cat] = non_reconcilable_cats.get(cat, 0) + 1
            db.update_transaction_status(txn.id, "IGNORED")

    # Filter to reconcilable — exclude BOTH non-reconcilable AND linked
    reconcilable_txns = [
        t for t in unmatched_txns
        if t.id not in non_reconcilable_ids and t.id not in linked_txn_ids
    ]

    # Read user's max date variance setting
    max_date_days = _get_max_date_days(db)

    # Pairs the user has already rejected — read ONCE per run and applied at
    # candidate generation, so a rejected pair is never proposed again.
    rejected_pairs = _load_rejected_pairs(db)

    # Run multi-pass reconciliation
    mp_result = find_matches_multi_pass(reconcilable_txns, unmatched_docs,
                                        max_date_days=max_date_days,
                                        rejected_pairs=rejected_pairs)

    # Build lookup maps for generating match explanations
    txn_map = {t.id: t for t in reconcilable_txns}
    doc_map = {d.id: d for d in unmatched_docs}

    # Store matches under the confidence floor / displacement rule
    threshold = auto_approve_threshold or AUTO_APPROVE_THRESHOLD
    store_counts = _store_matches(
        db, mp_result.matches, txn_map, doc_map, threshold,
        rejected_pairs=rejected_pairs,
    )
    auto_approved_count = store_counts["auto_approved"]
    pending_review_count = store_counts["pending_review"]

    # ── 4th Pass: LLM Transaction Linking (sync path) ──
    # Linkage is independent of matching — evaluate ALL transactions.
    # Skip only those that already have an active DB link (1:1 enforcement).
    fourth_pass_link_count = 0
    try:
        from core.llm_transaction_linker import run_llm_transaction_linking

        all_txns = db.get_all_transactions_for_linking()
        db_linked_ids = db.get_actively_linked_transaction_ids()
        # Merge DB links with links created by the deterministic linker this run
        all_linked_ids = db_linked_ids | linked_txn_ids

        logger.info(
            "4th pass input: %d total transactions (%d already linked, evaluating %d)",
            len(all_txns), len(all_linked_ids), len(all_txns) - len(all_linked_ids),
        )

        if len(all_txns) >= 2:
            llm_link_result = run_llm_transaction_linking(
                transactions=all_txns,
                already_linked_ids=all_linked_ids,
                # A pair the user unlinked by hand is not a candidate for this
                # pass either — the rejected link row outlives the unlink
                # precisely so both linkers can read it.
                rejected_pairs=db.get_user_rejected_link_pairs(),
            )
            for link in llm_link_result.links:
                try:
                    db.create_transaction_link(
                        source_txn_id=link.source_transaction_id,
                        target_txn_id=link.target_transaction_id,
                        link_type=link.link_type,
                        confidence_score=link.confidence_score,
                        match_source="AUTO",
                        explanation=f"[LLM-Pass4] {link.explanation}",
                        exchange_rate=link.exchange_rate,
                        amount_variance=link.amount_variance,
                        chain_id=link.chain_id,
                        chain_position=link.chain_position,
                    )
                    fourth_pass_link_count += 1
                    linked_txn_ids.add(link.source_transaction_id)
                    linked_txn_ids.add(link.target_transaction_id)
                except Exception as link_exc:
                    logger.debug("4th pass: skipped link %s->%s: %s",
                                 link.source_transaction_id, link.target_transaction_id, link_exc)
    except Exception as exc:
        logger.warning("4th pass LLM transaction linking failed (non-fatal): %s", exc)

    # Calculate stats — includes LINKED in reconciled count
    reconcilable_count = len(reconcilable_txns)
    # `matched` is what this run NEWLY WROTE, not what the engine proposed.
    # A pair already on file that this run only re-scored is `refreshed`:
    # a flagged pair stays in the candidate pool by design, so every later run
    # proposes it again, and counting those as matches would let a run that
    # stored nothing report new matches.
    matched_count = store_counts["stored"]
    refreshed_count = store_counts["refreshed"]
    flagged_count = store_counts["flagged"]
    proposed_count = len(mp_result.matches)

    total_matched = db.count_transactions_by_status("MATCHED")
    total_linked = db.count_transactions_by_status("LINKED")
    total_unmatched = db.count_transactions_by_status("UNMATCHED")
    total_reconcilable = total_matched + total_linked + total_unmatched
    total_reconciled = total_matched + total_linked
    rate = (total_reconciled / total_reconcilable * 100) if total_reconcilable > 0 else 0

    # Determine reconciliation outcome status
    if matched_count > 0 and matched_count >= reconcilable_count:
        status = "success"
    elif matched_count > 0:
        status = "partial"
    elif reconcilable_count == 0 and len(unmatched_docs) == 0:
        status = "nothing_to_reconcile"
    else:
        status = "no_matches"

    llm_stats = mp_result.llm_stats or {}
    result = {
        "status": status,
        "matched": matched_count,
        # The rest of the engine's proposals, so the delta is never hidden:
        # already on file and re-scored, or pairs the engine put forward at all.
        "refreshed": refreshed_count,
        "proposed": proposed_count,
        "auto_approved": auto_approved_count,
        "pending_review": pending_review_count,
        # Flagged instead of matched, and weak matches taken apart
        "flagged_for_review": flagged_count,
        "displaced": store_counts["displaced"],
        # How the AI agents fared, so a silent downgrade is impossible
        "llm_calls": llm_stats.get("calls", 0),
        "llm_fallbacks": llm_stats.get("fallbacks", 0),
        "llm_fallback_labels": llm_stats.get("by_label", {}),
        "linked": total_linked,
        "fourth_pass_links": fourth_pass_link_count,
        "reconcilable": reconcilable_count,
        "non_reconcilable": len(non_reconcilable_ids),
        "non_reconcilable_breakdown": non_reconcilable_cats,
        "rate": round(rate, 1),
        "remaining_transactions": reconcilable_count - matched_count,
        "remaining_documents": len(unmatched_docs) - matched_count,
        "pass_counts": mp_result.pass_counts,
        "matches": [
            {
                "transaction_id": m.transaction_id,
                "document_id": m.document_id,
                "confidence": m.confidence_score,
                "status": m.status.value,
                "match_type": m.match_type.value,
                "explanation": m.explanation,
            }
            for m in mp_result.matches
        ],
    }

    # Send SSE event
    try:
        from server.api.events import send_event
        send_event(user.id, "reconciliation_complete", {
            "matched": matched_count,
            "refreshed": refreshed_count,
            "flagged_for_review": flagged_count,
            "auto_approved": auto_approved_count,
            "pending_review": pending_review_count,
            "fourth_pass_links": fourth_pass_link_count,
            "rate": round(rate, 1),
        })
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Background (async) reconciliation with progress tracking
# ---------------------------------------------------------------------------

# How many recent completions the ETA averages over. Short enough to follow a
# run that speeds up (the verdict memory starts hitting) or slows down (the LLM
# endpoint picks up an extraction queue at the same time); long enough that one
# slow document does not double the estimate. Roughly one full wave of the
# matcher's concurrent workers.
_ETA_WINDOW = 10

# Below this many completions the rate is not yet a rate — the first few
# documents finish while the pool is still filling, so an ETA derived from them
# is wrong in an obvious, trust-destroying way. Report no ETA instead.
_ETA_MIN_SAMPLES = 3


class _EtaTracker:
    """A rolling estimate of how long the current phase has left.

    Deliberately phase-scoped rather than whole-run: the matcher's unit of work
    is a document and the linker's is a batch of candidate pairs, and averaging
    across the two would produce a number that is not an estimate of anything.
    ``phase`` names which unit the caller is counting, and the panel says so.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = ""
        self._total = 0
        self._done = 0
        # (timestamp, units finished by then). Units, not marks: the linker
        # advances by a whole batch at a time and the matcher by one document,
        # so a rate counted in marks would be wrong for one of them.
        self._marks: deque[tuple[float, int]] = deque(maxlen=_ETA_WINDOW)

    def begin(self, phase: str, total: int) -> None:
        with self._lock:
            self._phase = phase
            self._total = max(0, int(total))
            self._done = 0
            self._marks = deque([(time.time(), 0)], maxlen=_ETA_WINDOW)

    def complete(self, n: int = 1) -> None:
        with self._lock:
            self._done += n
            self._marks.append((time.time(), self._done))

    def set_done(self, done: int) -> None:
        """Absolute progress, for a phase that keeps its own counter.

        The 4th-pass linker counts pairs evaluated under its own lock, and its
        callback fires more than once per batch. Recording a mark only when the
        count actually moves keeps the rolling window a window over *work*,
        not over callbacks.
        """
        with self._lock:
            if done <= self._done:
                return
            self._done = done
            self._marks.append((time.time(), done))

    def snapshot(self) -> dict:
        """``{pairs_total, pairs_done, eta_seconds}`` — eta None when unknown."""
        with self._lock:
            total, done = self._total, self._done
            marks = list(self._marks)
        eta: int | None = None
        if total and done < total and len(marks) >= _ETA_MIN_SAMPLES:
            elapsed = marks[-1][0] - marks[0][0]
            units = marks[-1][1] - marks[0][1]
            if elapsed > 0 and units > 0:
                seconds_each = elapsed / units
                eta = int(round(seconds_each * (total - done)))
        return {
            "pairs_total": total,
            "pairs_done": min(done, total) if total else done,
            "eta_seconds": eta,
        }


def _llm_calls_done() -> int:
    """Engine LLM calls made so far this run, from the shared tally."""
    try:
        from core.llm_sync import llm_call_stats_snapshot
        return int(llm_call_stats_snapshot().get("calls", 0))
    except Exception:
        return 0


def _update_progress(user_id: str, phase: str, detail: str = "",
                     percent: int = 0, matches_so_far: int = 0,
                     status: str = "running", log: str | None = None,
                     eta: "_EtaTracker | None" = None) -> None:
    """Thread-safe update to the in-memory progress store.

    ``eta`` is the tracker for the phase now running; its counts and estimate
    are folded into the payload so ``/api/reconciliation/progress`` carries an
    honest countdown rather than only a percentage. When no tracker is passed —
    the short phases between the long ones — the last known counts are carried
    forward instead of being blanked, so the panel does not flicker.
    """
    with _progress_lock:
        existing = _reconciliation_progress.get(user_id, {})
        logs: list[str] = existing.get("logs", [])
        if log:
            logs.append(log)
        # Keep only last 200 log entries to bound memory
        if len(logs) > 200:
            logs = logs[-200:]
        if eta is not None:
            counters = eta.snapshot()
        else:
            counters = {
                "pairs_total": existing.get("pairs_total", 0),
                "pairs_done": existing.get("pairs_done", 0),
                "eta_seconds": existing.get("eta_seconds"),
            }
        _reconciliation_progress[user_id] = {
            "status": status,
            "phase": phase,
            "detail": detail,
            "percent": percent,
            "matches_so_far": matches_so_far,
            "updated_at": time.time(),
            "logs": logs,
            "llm_calls_done": _llm_calls_done(),
            **counters,
        }


def _run_reconciliation_background(user_id: str, db: DatabasePg,
                                   auto_approve_threshold: float | None = None,
                                   start_date: str | None = None,
                                   end_date: str | None = None,
                                   source_file: str | None = None) -> None:
    """Run full reconciliation in a background thread, updating progress store.

    Optional ISO-date `start_date` / `end_date` scope the candidate pool to a
    window — same semantics as `_run_reconciliation_internal` above.

    `source_file` scopes the **transactions** to a single statement tab (the
    per-statement "Reconcile Statement" action). The **document** pool stays the
    full set of unmatched receipts so a statement's transactions are matched
    against every available receipt — only the transactions are shortlisted to
    the selected statement. When statement-scoped, the global cross-statement
    4th-pass LLM linker is skipped (it is inherently a whole-account operation
    and would contradict "reconcile only this statement").
    """

    try:
        with _user_reconcile_lock(db) as lock_acquired:
            if not lock_acquired:
                # Another run (possibly another process, or the sync /run
                # path) holds the per-user lock. Don't touch the DB and
                # don't clobber the active run's progress entry.
                logger.warning(
                    "Reconciliation for user %s skipped: advisory lock held by another run",
                    user_id,
                )
                return
            _run_reconciliation_background_locked(
                user_id, db,
                auto_approve_threshold=auto_approve_threshold,
                start_date=start_date, end_date=end_date,
                source_file=source_file,
            )
    except Exception:
        logger.exception("Background reconciliation failed for user %s", user_id)
        tb_lines = traceback.format_exc().splitlines()
        error_detail = tb_lines[-1] if tb_lines else "Unknown error"
        with _progress_lock:
            existing = _reconciliation_progress.get(user_id, {})
            logs = existing.get("logs", [])
            logs.append(f"ERROR: {error_detail}")
            for line in tb_lines[-3:]:
                logs.append(f"  {line.strip()}")
            _reconciliation_progress[user_id] = {
                "status": "error",
                "phase": "Failed",
                "detail": error_detail,
                "percent": 0,
                "matches_so_far": 0,
                "updated_at": time.time(),
                "logs": logs,
            }


def _run_reconciliation_background_locked(
        user_id: str, db: DatabasePg,
        auto_approve_threshold: float | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        source_file: str | None = None) -> None:
    """Body of the background run. Caller holds the per-user advisory lock."""
    from core.adjudication_memory import AdjudicationMemory
    from core.reconciliation import find_matches_multi_pass

    # One tracker for the whole run; each long phase re-arms it with its own
    # unit of work (documents, then 4th-pass candidate pairs).
    eta = _EtaTracker()

    try:
        _update_progress(user_id, "Categorizing transactions...", percent=5,
                         log="Fetching unmatched transactions and documents...")

        unmatched_txns = db.get_unmatched_transactions(
            since=start_date, until=end_date, source_file=source_file
        )
        # Documents are NOT scoped to the statement — a statement's transactions
        # match against the full pool of unmatched (ready-state) receipts.
        unmatched_docs = db.get_unmatched_documents(since=start_date, until=end_date)

        scope_note = f" (statement: {source_file})" if source_file else ""
        _update_progress(user_id, "Categorizing transactions...", percent=5,
                         log=f"Found {len(unmatched_txns)} unmatched transactions{scope_note} and {len(unmatched_docs)} unmatched documents")

        if len(unmatched_txns) == 0 and len(unmatched_docs) == 0:
            # Terminal state — without the return, the run continues through
            # empty categorize/link/match passes while pollers already saw
            # percent=100 and closed their progress UI.
            with _progress_lock:
                existing = _reconciliation_progress.get(user_id, {})
                logs = existing.get("logs", [])
                logs.append("No unmatched transactions or documents found.")
                _reconciliation_progress[user_id] = {
                    "status": "complete",
                    "phase": "Nothing to reconcile",
                    "detail": "",
                    "percent": 100,
                    "matches_so_far": 0,
                    "updated_at": time.time(),
                    "logs": logs,
                    "result": {
                        "status": "no_data",
                        "matched": 0,
                        "refreshed": 0,
                        "proposed": 0,
                        "auto_approved": 0,
                        "pending_review": 0,
                        "reconcilable": 0,
                        "non_reconcilable": 0,
                        "linked": 0,
                        "fourth_pass_links": 0,
                        "rate": 0,
                        "remaining_transactions": 0,
                        "remaining_documents": 0,
                        "total_transactions": 0,
                        "total_documents": 0,
                        "pass_counts": {},
                    },
                }
            return

        categories = _auto_categorize_transactions(unmatched_txns)  # canonical labels for the histogram

        # ── Run cross-statement transaction linking ──
        #
        # Runs BEFORE the non-reconcilable stamping, over the whole book rather
        # than this run's scope — including on a statement-scoped run, where the
        # counterpart leg is by construction on another statement. "Credit card
        # payment" is itself a non-reconcilable rule, so stamping first would
        # take a leg out of the pool before its counterpart was ever considered.
        # See DatabasePg.get_linkable_transactions.
        linked_txn_ids: set[int] = set()
        try:
            from core.transaction_linker import run_transaction_linking
            linker_result = run_transaction_linking(
                transactions=db.get_linkable_transactions(),
                rejected_pairs=db.get_user_rejected_link_pairs(),
            )
            for link in linker_result.links:
                try:
                    db.create_transaction_link(
                        source_txn_id=link.source_transaction_id,
                        target_txn_id=link.target_transaction_id,
                        link_type=link.link_type,
                        confidence_score=link.confidence_score,
                        match_source="AUTO",
                        explanation=link.explanation,
                        exchange_rate=link.exchange_rate,
                        amount_variance=link.amount_variance,
                        # Keep the fan-out group intact across the DB boundary.
                        chain_id=link.chain_id,
                        chain_position=link.chain_position,
                    )
                    linked_txn_ids.add(link.source_transaction_id)
                    linked_txn_ids.add(link.target_transaction_id)
                except Exception as link_exc:
                    logger.warning("Failed to create link %s->%s: %s",
                                   link.source_transaction_id, link.target_transaction_id, link_exc)
            if linker_result.links:
                _update_progress(user_id, "Transaction linking...", percent=9,
                                 log=f"Auto-linked {len(linker_result.links)} cross-statement transfer pairs "
                                     f"({len(linker_result.auto_approved)} auto-approved, "
                                     f"{len(linker_result.pending_review)} pending review)")
        except Exception as exc:
            logger.warning("Transaction linking failed (continuing with reconciliation): %s", exc)

        # Identify non-reconcilable transactions. A row the linker just explained
        # is skipped: it is LINKED now, and stamping it IGNORED would overwrite a
        # real explanation with "no receipt needed" and hide the link in the grid.
        non_reconcilable_ids = set()
        non_reconcilable_cats: dict[str, int] = {}
        for txn in unmatched_txns:
            if txn.id not in linked_txn_ids and is_non_reconcilable(txn.description):
                cat = categories.get(txn.id, "Uncategorized")
                non_reconcilable_ids.add(txn.id)
                non_reconcilable_cats[cat] = non_reconcilable_cats.get(cat, 0) + 1
                db.update_transaction_status(txn.id, "IGNORED")

        # Filter to reconcilable — exclude BOTH non-reconcilable AND linked
        reconcilable_txns = [
            t for t in unmatched_txns
            if t.id not in non_reconcilable_ids and t.id not in linked_txn_ids
        ]

        if non_reconcilable_ids:
            # percent 9, not 8: the linking log above already reported 9 and a
            # progress bar must never walk backwards.
            _update_progress(user_id, "Categorizing transactions...", percent=9,
                             log=f"Auto-categorized {len(non_reconcilable_ids)} non-reconcilable transactions (bank fees, transfers, etc.)")

        # Read user's max date variance setting
        max_date_days = _get_max_date_days(db)

        # Pairs the user has already rejected — read ONCE per run, applied at
        # candidate generation inside the engine.
        rejected_pairs = _load_rejected_pairs(db)
        if rejected_pairs:
            _update_progress(
                user_id, "Starting reconciliation...", percent=10,
                log=(
                    f"Honouring {len(rejected_pairs)} previously rejected "
                    f"pair(s) — they are excluded from the candidate pool"
                ),
            )

        _update_progress(
            user_id,
            "Starting reconciliation...",
            detail=f"{len(reconcilable_txns)} transactions, {len(unmatched_docs)} documents",
            percent=10,
            log=f"Reconciling {len(reconcilable_txns)} transactions against {len(unmatched_docs)} documents (max date variance: {max_date_days} days)...",
        )

        # Verdicts this user's earlier runs already got from the model, read
        # once here rather than from each worker thread. An agent whose question
        # is unchanged reuses the stored reply; everything else asks the model.
        # Never let the memory cost a run: a database without the memory table,
        # or a caller passing a narrower db object, must still reconcile — it
        # just asks the model every question.
        _memory_reader = getattr(db, "get_adjudication_memory", None)
        memory = AdjudicationMemory(
            _memory_reader([d.id for d in unmatched_docs if d.id])
            if callable(_memory_reader) else [],
        )
        if memory.stats()["known"]:
            _update_progress(
                user_id, "Starting reconciliation...", percent=12,
                log=(
                    f"{memory.stats()['known']} remembered agent verdict(s) "
                    f"available — any pair that has not changed is settled "
                    f"without asking the model again"
                ),
            )

        # Progress callback for find_matches_multi_pass
        # Counter owned exclusively by this callback — increments when a worker signals is_final
        _pass_docs_completed = [0]
        _pass_counter_lock = threading.Lock()
        eta.begin("documents", len(unmatched_docs))

        def _on_pass_progress(info: dict) -> None:
            matches = info.get("matches_so_far", 0)
            # Per-document progress from find_matches()
            log_msg = info.get("log")
            if log_msg:
                total = info.get("total_docs", len(unmatched_docs))
                # Increment counter when a doc finishes (worker signals is_final)
                if info.get("is_final"):
                    with _pass_counter_lock:
                        _pass_docs_completed[0] += 1
                    eta.complete()
                completed = _pass_docs_completed[0]
                pct = 15 + int(65 * completed / max(total, 1))  # 15%–80%
                # Prepend sequential counter to the log message
                prefixed_log = f"[{completed}/{total}] {log_msg}"
                _update_progress(
                    user_id,
                    "3-agent reconciliation running...",
                    detail=f"{matches} matched so far — processed {completed}/{total} docs",
                    percent=min(pct, 80),
                    matches_so_far=matches,
                    log=prefixed_log,
                    eta=eta,
                )
            else:
                _update_progress(
                    user_id,
                    "3-agent reconciliation running...",
                    detail="Amount → Date → Vendor agents evaluating each proof",
                    percent=15,
                    matches_so_far=matches,
                    log=f"Running 3-agent pipeline (Amount → Date → Vendor) on {len(unmatched_docs)} proofs of transaction...",
                    eta=eta,
                )

        mp_result = find_matches_multi_pass(
            reconcilable_txns, unmatched_docs,
            progress_callback=_on_pass_progress,
            max_date_days=max_date_days,
            rejected_pairs=rejected_pairs,
            memory=memory,
        )

        # Persist what the model said this run, so the next one does not ask
        # again. After the pool has drained: the matcher never touches the DB.
        _new_verdicts = memory.pending()
        _memory_writer = getattr(db, "save_adjudication_memory", None)
        if _new_verdicts and callable(_memory_writer):
            stored = _memory_writer(_new_verdicts)
            logger.info(
                "Reconciliation: remembered %d new agent verdict(s) (%d reused this run)",
                stored, memory.hits,
            )
        if memory.hits:
            _update_progress(
                user_id, "Matching complete", percent=84,
                matches_so_far=len(mp_result.matches),
                log=(
                    f"{memory.hits} of {memory.hits + memory.misses} agent question(s) "
                    f"were answered from memory — unchanged pairs did not go to the model"
                ),
            )

        txn_map = {t.id: t for t in reconcilable_txns}
        doc_map = {d.id: d for d in unmatched_docs}

        _update_progress(
            user_id, "Matching complete",
            percent=85,
            matches_so_far=len(mp_result.matches),
            log=f"3-agent pipeline complete: {len(mp_result.matches)} candidate pair(s) proposed",
        )

        # Store results phase
        _update_progress(
            user_id, "Storing results...",
            detail=f"{len(mp_result.matches)} candidate pair(s) to save",
            percent=85,
            matches_so_far=len(mp_result.matches),
            log=f"Matching complete — saving {len(mp_result.matches)} candidate pair(s) to database...",
        )

        threshold = auto_approve_threshold or AUTO_APPROVE_THRESHOLD
        store_counts = _store_matches(
            db, mp_result.matches, txn_map, doc_map, threshold,
            rejected_pairs=rejected_pairs,
        )
        auto_approved_count = store_counts["auto_approved"]
        pending_review_count = store_counts["pending_review"]
        _update_progress(
            user_id, "Storing results...", percent=86,
            matches_so_far=store_counts["stored"],
            log=(
                f"{store_counts['stored']} new match(es) written; "
                f"{store_counts['refreshed']} pair(s) already on file were "
                f"re-scored, not counted as new"
            ),
        )
        if store_counts["user_rejected_skipped"]:
            _update_progress(
                user_id, "Storing results...", percent=86,
                matches_so_far=len(mp_result.matches),
                log=(
                    f"{store_counts['user_rejected_skipped']} match(es) not stored: "
                    f"the user had already rejected that pair"
                ),
            )
        if store_counts["flagged"]:
            _update_progress(
                user_id, "Storing results...", percent=86,
                matches_so_far=len(mp_result.matches),
                log=(
                    f"{store_counts['flagged']} match(es) flagged for review instead "
                    f"of matched (low confidence or vendor mismatch) - the "
                    f"transactions stay in the pool"
                ),
            )
        if store_counts["displaced"]:
            _update_progress(
                user_id, "Storing results...", percent=86,
                matches_so_far=len(mp_result.matches),
                log=(
                    f"{store_counts['displaced']} weak existing match(es) displaced by "
                    f"a stronger one; their proofs are unmatched again"
                ),
            )

        # ── 4th Pass: LLM Transaction Linking ──
        # Skipped for statement-scoped reconciles — cross-statement linking is a
        # whole-account operation that would reach beyond the selected statement.
        fourth_pass_link_count = 0
        try:
            if source_file is not None:
                _update_progress(user_id, "Cross-statement linking...", percent=89,
                                 log="4th pass: skipped (statement-scoped reconcile)")
                raise _SkipFourthPass()

            _update_progress(user_id, "Cross-statement linking...",
                             percent=86, matches_so_far=len(mp_result.matches),
                             log="Running 4th pass: LLM analysis of all transactions for cross-statement links...")

            from core.llm_transaction_linker import run_llm_transaction_linking

            # Linkage is independent of matching — evaluate ALL transactions.
            # Skip only those that already have an active DB link (1:1 enforcement).
            all_txns = db.get_all_transactions_for_linking()
            db_linked_ids = db.get_actively_linked_transaction_ids()
            all_linked_ids = db_linked_ids | linked_txn_ids

            logger.info(
                "4th pass input: %d total transactions (%d already linked, evaluating %d)",
                len(all_txns), len(all_linked_ids), len(all_txns) - len(all_linked_ids),
            )

            # Progress callback for 4th pass — pipes logs to the reconciliation log window.
            # Batches now run several at a time, so this is called from worker
            # threads; `pairs_evaluated` is the linker's own locked counter and
            # the ETA is re-based from it rather than counted here.
            _fourth_pass_started = [False]

            def _on_4th_pass_progress(info: dict) -> None:
                log_msg = info.get("log")
                log_details = info.get("log_details", [])
                total_batches = info.get("total_batches", 1)
                batch = info.get("batch", 1)
                total_pairs = info.get("total_pairs")
                evaluated = info.get("pairs_evaluated")
                if total_pairs and not _fourth_pass_started[0]:
                    _fourth_pass_started[0] = True
                    eta.begin("candidate pairs", int(total_pairs))
                if evaluated is not None and _fourth_pass_started[0]:
                    eta.set_done(int(evaluated))
                pct = 86 + int(3 * batch / max(total_batches, 1))  # 86%–89%
                if log_msg:
                    _update_progress(
                        user_id, "Cross-statement linking...",
                        percent=min(pct, 89),
                        matches_so_far=len(mp_result.matches),
                        log=log_msg,
                        eta=eta if _fourth_pass_started[0] else None,
                    )
                # Emit each pair detail as its own log line for real-time visibility
                for detail in log_details:
                    _update_progress(
                        user_id, "Cross-statement linking...",
                        percent=min(pct, 89),
                        matches_so_far=len(mp_result.matches),
                        log=detail,
                        eta=eta if _fourth_pass_started[0] else None,
                    )

            if len(all_txns) >= 2:
                llm_link_result = run_llm_transaction_linking(
                    transactions=all_txns,
                    already_linked_ids=all_linked_ids,
                    progress_callback=_on_4th_pass_progress,
                    # A pair the user unlinked by hand is not a candidate for
                    # this pass either — the rejected link row outlives the
                    # unlink precisely so both linkers can read it.
                    rejected_pairs=db.get_user_rejected_link_pairs(),
                )
                for link in llm_link_result.links:
                    try:
                        db.create_transaction_link(
                            source_txn_id=link.source_transaction_id,
                            target_txn_id=link.target_transaction_id,
                            link_type=link.link_type,
                            confidence_score=link.confidence_score,
                            match_source="AUTO",
                            explanation=f"[LLM-Pass4] {link.explanation}",
                            exchange_rate=link.exchange_rate,
                            amount_variance=link.amount_variance,
                            chain_id=link.chain_id,
                            chain_position=link.chain_position,
                        )
                        fourth_pass_link_count += 1
                        linked_txn_ids.add(link.source_transaction_id)
                        linked_txn_ids.add(link.target_transaction_id)
                    except Exception as link_exc:
                        logger.debug("4th pass: skipped link %s->%s: %s",
                                     link.source_transaction_id, link.target_transaction_id, link_exc)

                if fourth_pass_link_count > 0:
                    _update_progress(user_id, "Cross-statement linking...", percent=89,
                                     log=f"4th pass: linked {fourth_pass_link_count} pairs via LLM analysis")
                else:
                    _update_progress(user_id, "Cross-statement linking...", percent=89,
                                     log=f"4th pass: evaluated {llm_link_result.total_candidates_evaluated} pairs, no links met threshold")
            else:
                _update_progress(user_id, "Cross-statement linking...", percent=89,
                                 log="4th pass: skipped (fewer than 2 transactions)")
        except _SkipFourthPass:
            pass  # statement-scoped reconcile — intentional skip, already logged
        except Exception as exc:
            logger.warning("4th pass LLM transaction linking failed (non-fatal): %s", exc)

        # Calculate final stats
        reconcilable_count = len(reconcilable_txns)
        # Same rule as the sync path: `matched` is what this run newly wrote.
        # Re-scoring a pair that was already on file is `refreshed` — it is not
        # a new match, and reporting it as one would let a repeat run on the
        # same statement claim it had found something.
        matched_count = store_counts["stored"]
        refreshed_count = store_counts["refreshed"]
        flagged_count = store_counts["flagged"]
        proposed_count = len(mp_result.matches)

        # Cumulative rate (same as sync path) — includes LINKED in reconciled count
        total_matched = db.count_transactions_by_status("MATCHED")
        total_linked = db.count_transactions_by_status("LINKED")
        total_unmatched = db.count_transactions_by_status("UNMATCHED")
        total_reconcilable = total_matched + total_linked + total_unmatched
        total_reconciled = total_matched + total_linked
        rate = (total_reconciled / total_reconcilable * 100) if total_reconcilable > 0 else 0

        if matched_count > 0 and matched_count >= reconcilable_count:
            final_status = "success"
        elif matched_count > 0:
            final_status = "partial"
        elif reconcilable_count == 0 and len(unmatched_docs) == 0:
            final_status = "nothing_to_reconcile"
        else:
            final_status = "no_matches"

        # ── Auto-categorization via the categorization agent ──
        _update_progress(user_id, "Categorizing transactions (this may take a while)...",
                         percent=90, matches_so_far=matched_count,
                         log="Running categorization agent...")

        try:
            from core.categorization_agent import (
                ReconciliationProgressReporter,
                run_categorization_agent,
            )
            reporter = ReconciliationProgressReporter(
                user_id=user_id, base_percent=90, percent_span=5,
            )
            run_categorization_agent(
                db=db, user_id=user_id, scope="uncategorized", progress=reporter,
            )
        except Exception as e:
            logger.warning(f"Categorization agent failed: {e}")
            _update_progress(user_id, "Categorization skipped", percent=95,
                             matches_so_far=matched_count,
                             log=f"Categorization agent failed: {e}")

        _update_progress(user_id, "Complete", percent=95,
                         matches_so_far=matched_count,
                         log=(f"Done! {matched_count} new match(es), "
                              f"{refreshed_count} re-scored, "
                              f"{auto_approved_count} auto-approved, "
                              f"{pending_review_count} pending review "
                              f"({round(rate, 1)}% rate)"))

        # Write final progress
        with _progress_lock:
            existing = _reconciliation_progress.get(user_id, {})
            final_logs = existing.get("logs", [])
            _reconciliation_progress[user_id] = {
                "status": "complete",
                "phase": "Complete",
                "detail": "",
                "percent": 100,
                "matches_so_far": matched_count,
                "updated_at": time.time(),
                "logs": final_logs,
                "result": {
                    "status": final_status,
                    "matched": matched_count,
                    # The rest of the engine's proposals, so the delta between
                    # "proposed" and "newly stored" is never hidden.
                    "refreshed": refreshed_count,
                    "proposed": proposed_count,
                    "auto_approved": auto_approved_count,
                    "pending_review": pending_review_count,
                    # Flagged instead of matched, and weak matches taken
                    # apart by a better candidate
                    "flagged_for_review": flagged_count,
                    "displaced": store_counts["displaced"],
                    # How the AI agents fared this run
                    "llm_calls": (mp_result.llm_stats or {}).get("calls", 0),
                    "llm_fallbacks": (mp_result.llm_stats or {}).get("fallbacks", 0),
                    "llm_fallback_labels": (mp_result.llm_stats or {}).get("by_label", {}),
                    "reconcilable": reconcilable_count,
                    "non_reconcilable": len(non_reconcilable_ids),
                    "linked": total_linked,
                    "fourth_pass_links": fourth_pass_link_count,
                    "rate": round(rate, 1),
                    "remaining_transactions": reconcilable_count - matched_count,
                    "remaining_documents": len(unmatched_docs) - matched_count,
                    "total_transactions": total_reconcilable,
                    "total_documents": len(unmatched_docs),
                    "pass_counts": mp_result.pass_counts,
                },
            }

        # Send SSE event (best effort)
        try:
            from server.api.events import send_event
            send_event(user_id, "reconciliation_complete", {
                "matched": matched_count,
                "refreshed": refreshed_count,
                "flagged_for_review": flagged_count,
                "auto_approved": auto_approved_count,
                "pending_review": pending_review_count,
                "fourth_pass_links": fourth_pass_link_count,
                "rate": round(rate, 1),
            })
        except Exception:
            pass

    except Exception:
        logger.exception("Background reconciliation failed for user %s", user_id)
        tb_lines = traceback.format_exc().splitlines()
        error_detail = tb_lines[-1] if tb_lines else "Unknown error"
        with _progress_lock:
            existing = _reconciliation_progress.get(user_id, {})
            logs = existing.get("logs", [])
            logs.append(f"ERROR: {error_detail}")
            # Include last 3 lines of traceback for debugging
            for line in tb_lines[-3:]:
                logs.append(f"  {line.strip()}")
            _reconciliation_progress[user_id] = {
                "status": "error",
                "phase": "Failed",
                "detail": error_detail,
                "percent": 0,
                "matches_so_far": 0,
                "updated_at": time.time(),
                "logs": logs,
            }


@router.get("/settings")
async def get_reconciliation_settings(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return user-configurable reconciliation settings."""
    max_date_days = _get_max_date_days(db)
    return {
        "max_date_variance_days": max_date_days,
        "options": MAX_DATE_DAYS_OPTIONS,
    }


class ReconciliationSettingsUpdate(BaseModel):
    max_date_variance_days: int


@router.put("/settings")
async def update_reconciliation_settings(
    body: ReconciliationSettingsUpdate,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Update user-configurable reconciliation settings."""
    if body.max_date_variance_days not in MAX_DATE_DAYS_OPTIONS:
        raise HTTPException(400, f"max_date_variance_days must be one of {MAX_DATE_DAYS_OPTIONS}")

    profile = db.get_business_profile() or {}
    config = profile.get("config_json") or {}
    config["max_date_variance_days"] = body.max_date_variance_days
    db.upsert_business_profile({"config_json": config})

    return {"max_date_variance_days": body.max_date_variance_days}


@router.post("/start")
async def start_reconciliation_async(
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Start reconciliation in the background and return immediately.

    Poll ``GET /api/reconciliation/progress`` to track status.
    """
    uid = user.id

    # Atomic claim — prevents duplicate runs in this process; the background
    # worker additionally takes a per-user PG advisory lock so concurrent
    # runs from other processes (or the sync /run path) no-op safely.
    existing = _try_claim_run(uid)
    if existing is not None:
        return {"status": "already_running", "progress": existing}

    thread = threading.Thread(
        target=_run_reconciliation_background,
        args=(uid, db),
        daemon=True,
    )
    thread.start()

    return {"status": "started"}


class StatementReconcileRequest(BaseModel):
    """Body for a per-statement reconcile — `source_file` identifies the
    statement tab the user selected (e.g. ``bmo_cad_may2026`` or
    ``wise_USD_202605``)."""

    source_file: str


@router.post("/start-statement")
async def start_statement_reconciliation(
    body: StatementReconcileRequest,
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Reconcile ONLY the transactions of a single statement tab.

    Transactions are shortlisted to ``body.source_file`` (the selected statement
    bubble), but they are matched against the FULL pool of unmatched receipts —
    so receipts from any period can still match this statement's transactions.
    Runs in the background; poll ``GET /api/reconciliation/progress`` to track.
    """
    uid = user.id

    # Atomic claim — mirrors /start
    existing = _try_claim_run(uid)
    if existing is not None:
        return {"status": "already_running", "progress": existing}

    thread = threading.Thread(
        target=_run_reconciliation_background,
        args=(uid, db),
        kwargs={"source_file": body.source_file},
        daemon=True,
    )
    thread.start()

    return {"status": "started"}


@router.post("/reset-and-start")
async def reset_and_reconcile(
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Reset all reconciliation data and re-reconcile from scratch.

    1. Delete all reconciliation_matches for this user
    2. Delete all ledger_entries for this user
    3. Reset MATCHED/IGNORED transactions → UNMATCHED
    4. Reset MATCHED documents → EXTRACTED
    5. Start fresh reconciliation in background

    Deliberately **not** reset: the remembered agent verdicts. They are not
    decisions about the books — they are what the model said about a set of
    unchanged rows, and they are still the correct answer to the same question.
    Keeping them is what makes a reset-and-re-run quick; a pair whose
    transaction or document actually changed hashes differently and is re-asked.
    """
    uid = user.id

    # Atomic claim — mirrors /start
    existing = _try_claim_run(uid)
    if existing is not None:
        return {"status": "already_running", "progress": existing}

    _update_progress(uid, "Resetting all matches...", percent=0,
                     log="Clearing all previous reconciliation results...")

    # Reset everything in the database
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Delete ledger entries first (FK to reconciliation_matches)
            cur.execute("DELETE FROM ledger_entries WHERE user_id = %s", (uid,))
            ledger_deleted = cur.rowcount

            # Delete all reconciliation matches
            cur.execute("DELETE FROM reconciliation_matches WHERE user_id = %s", (uid,))
            matches_deleted = cur.rowcount

            # Delete all transaction links
            cur.execute("DELETE FROM transaction_links WHERE user_id = %s", (uid,))
            links_deleted = cur.rowcount

            # Reset transactions: MATCHED/IGNORED/LINKED → UNMATCHED
            cur.execute(
                "UPDATE transactions SET status = 'UNMATCHED' WHERE user_id = %s AND status IN ('MATCHED', 'IGNORED', 'LINKED')",
                (uid,),
            )
            txns_reset = cur.rowcount

            # Reset documents: MATCHED → EXTRACTED
            cur.execute(
                "UPDATE documents SET status = 'EXTRACTED' WHERE user_id = %s AND status = 'MATCHED'",
                (uid,),
            )
            docs_reset = cur.rowcount

            conn.commit()

    _update_progress(uid, "Reset complete, starting reconciliation...", percent=2,
                     log=f"Reset: {matches_deleted} matches deleted, {links_deleted} links deleted, {txns_reset} transactions reset, {docs_reset} documents reset, {ledger_deleted} ledger entries deleted")

    # Now start reconciliation in background
    thread = threading.Thread(
        target=_run_reconciliation_background,
        args=(uid, db),
        daemon=True,
    )
    thread.start()

    return {"status": "started", "reset": {
        "matches_deleted": matches_deleted,
        "transactions_reset": txns_reset,
        "documents_reset": docs_reset,
        "ledger_entries_deleted": ledger_deleted,
    }}


@router.get("/progress")
async def get_reconciliation_progress(
    user: AuthUser = Depends(get_current_user),
):
    """Return the current reconciliation progress for the authenticated user.

    Returns ``{status, phase, detail, percent, matches_so_far}`` plus the
    countdown fields the panel needs for a run that takes half an hour:

    * ``pairs_total`` / ``pairs_done`` — work units in the phase now running.
      The unit is a document while the 3-agent matcher is going and a
      candidate pair during cross-statement linking, which is why ``phase``
      has to be read alongside them.
    * ``llm_calls_done`` — engine LLM calls made so far this run.
    * ``eta_seconds`` — a rolling-average estimate for the phase now running,
      or ``null`` while there are too few completions to estimate honestly.

    When ``status`` is ``"complete"``, includes ``result`` with full stats.
    """
    with _progress_lock:
        progress = _reconciliation_progress.get(user.id)

        # Clean up terminal entries (complete/error) older than 30 minutes
        # to prevent unbounded memory growth across many users
        stale_cutoff = time.time() - 1800
        stale_keys = [
            uid for uid, p in _reconciliation_progress.items()
            if p.get("status") in ("complete", "error")
            and p.get("updated_at", 0) < stale_cutoff
        ]
        for uid in stale_keys:
            del _reconciliation_progress[uid]

    if progress is None:
        return {"status": "idle"}

    return progress


@router.post("/run")
async def run_reconciliation(
    req: ReconcileRequest | None = None,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Run multi-pass reconciliation on all unmatched transactions and documents."""
    # Per-user advisory lock to prevent concurrent reconciliation (CWE-367).
    # Shared with the async background path so sync and async runs mutually
    # exclude. The key is a hash of the user id rather than Python's hash(),
    # which is PYTHONHASHSEED-randomized per process, and the lock is released
    # on the same pooled connection that took it — a session lock released on
    # any other connection is not released at all.
    with _user_reconcile_lock(db) as acquired:
        if not acquired:
            raise HTTPException(status_code=409, detail="Reconciliation already in progress for this user")
        threshold = req.auto_approve_threshold if req else None
        start_date = req.start_date if req else None
        end_date = req.end_date if req else None
        return await _run_reconciliation_internal(
            user, db,
            auto_approve_threshold=threshold,
            start_date=start_date, end_date=end_date,
        )


@router.get("/summary")
async def reconciliation_summary(
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Return the current reconciliation state — how many matched, unmatched, etc."""
    with db._conn() as conn:
        with conn.cursor() as cur:
            uid = db.user_id

            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE status = 'MATCHED' AND user_id = %s",
                (uid,),
            )
            matched = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE status = 'UNMATCHED' AND user_id = %s",
                (uid,),
            )
            unmatched_txn = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE status = 'LINKED' AND user_id = %s",
                (uid,),
            )
            linked = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE status = 'IGNORED' AND user_id = %s",
                (uid,),
            )
            non_reconcilable = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM documents WHERE status = 'EXTRACTED' AND user_id = %s",
                (uid,),
            )
            unmatched_doc = cur.fetchone()[0]

            reconcilable = matched + unmatched_txn + linked
            reconciled = matched + linked
            rate = (reconciled / reconcilable * 100) if reconcilable > 0 else 0

            return {
                "matched": matched,
                "linked": linked,
                "reconcilable": reconcilable,
                "non_reconcilable": non_reconcilable,
                "unmatched_transactions": unmatched_txn,
                "unmatched_documents": unmatched_doc,
                "rate": round(rate, 1),
            }


@router.get("/matches")
async def list_matches(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    status: str | None = None,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Paginated list of reconciliation matches with transaction/document joins.

    Each match includes the associated transaction description, date, amount,
    and the document vendor, date, total. Also returns queue_counts for
    the review sidebar.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            # SAFETY: Dynamic WHERE clause is safe — all conditions are hardcoded
            # SQL fragments with %s placeholders. User-supplied status values are
            # passed as parameterized values, never interpolated into SQL structure.
            # The IN clause uses ",".join(["%s"] * N) which only produces
            # "%s,%s,..." — no user input in the SQL text.
            where_clauses = ["rm.user_id = %s"]
            params: list = [db.user_id]

            if status:
                # Support comma-separated statuses (e.g., "AUTO_APPROVED,USER_APPROVED")
                statuses = [s.strip() for s in status.split(",")]
                if len(statuses) == 1:
                    where_clauses.append("rm.status = %s")
                    params.append(statuses[0])
                else:
                    placeholders = ",".join(["%s"] * len(statuses))
                    where_clauses.append(f"rm.status IN ({placeholders})")
                    params.extend(statuses)

            where_sql = " AND ".join(where_clauses)

            # Count
            cur.execute(
                f"SELECT COUNT(*) FROM reconciliation_matches rm WHERE {where_sql}",
                params,
            )
            total = cur.fetchone()[0]

            # Queue counts (for review sidebar) — use same JOINs as the
            # data query so RLS on transactions/documents is reflected.
            cur.execute(
                """SELECT rm.status, COUNT(*)
                   FROM reconciliation_matches rm
                   LEFT JOIN transactions t ON rm.transaction_id = t.id
                   LEFT JOIN documents d ON rm.document_id = d.id
                   WHERE rm.user_id = %s
                   GROUP BY rm.status""",
                (db.user_id,),
            )
            queue_counts = {row[0]: row[1] for row in cur.fetchall()}

            # Paginated results with joins
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            cur.execute(
                f"""SELECT rm.id, rm.transaction_id, rm.document_id,
                           rm.confidence_score, rm.amount_score, rm.date_score,
                           rm.vendor_score, rm.match_type, rm.status,
                           rm.explanation, rm.user_action, rm.actioned_at,
                           rm.reviewed_by, rm.created_at,
                           t.description AS txn_description,
                           t.date_posted AS txn_date,
                           t.amount AS txn_amount,
                           t.currency AS txn_currency,
                           t.account AS txn_account,
                           d.vendor AS doc_vendor,
                           d.document_date AS doc_date,
                           d.total AS doc_total,
                           d.currency AS doc_currency,
                           d.original_filename AS doc_filename,
                           d.stored_path AS doc_stored_path
                    FROM reconciliation_matches rm
                    LEFT JOIN transactions t ON rm.transaction_id = t.id
                    LEFT JOIN documents d ON rm.document_id = d.id
                    WHERE {where_sql}
                    ORDER BY rm.created_at DESC
                    LIMIT %s OFFSET %s""",
                params,
            )

            matches = []
            for row in cur.fetchall():
                matches.append({
                    "id": row[0],
                    "transaction_id": row[1],
                    "document_id": row[2],
                    "confidence_score": row[3],
                    "amount_score": row[4],
                    "date_score": row[5],
                    "vendor_score": row[6],
                    "match_type": row[7],
                    "status": row[8],
                    "explanation": row[9],
                    "user_action": row[10],
                    "actioned_at": str(row[11]) if row[11] else None,
                    "reviewed_by": row[12],
                    "created_at": str(row[13]) if row[13] else None,
                    "transaction": {
                        "description": row[14],
                        "date": str(row[15]) if row[15] else None,
                        "amount": str(row[16]) if row[16] else None,
                        "currency": row[17],
                        "account": row[18],
                    },
                    "document": {
                        "vendor": row[19],
                        "date": str(row[20]) if row[20] else None,
                        "total": str(row[21]) if row[21] else None,
                        "currency": row[22],
                        "filename": row[23],
                        "stored_path": row[24],
                    },
                })

            return {
                "matches": matches,
                "total": total,
                "page": page,
                "per_page": per_page,
                "queue_counts": queue_counts,
            }


@router.patch("/matches/{match_id}/approve")
async def approve_match(
    match_id: int,
    bundle: bool = Query(False),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Approve a reconciliation match.

    With ?bundle=true, approves ALL matches sharing the same group_id.
    Sets status to USER_APPROVED, records user_action and actioned_at,
    and logs the action in the audit_log.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Verify match exists
            cur.execute(
                "SELECT id, status, group_id FROM reconciliation_matches WHERE id = %s AND user_id = %s",
                (match_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Match not found")

            old_status = row[1]
            group_id = row[2]

            # Determine targets
            if bundle and group_id:
                cur.execute(
                    """SELECT id FROM reconciliation_matches
                       WHERE group_id = %s AND user_id = %s AND status = 'PENDING_REVIEW'""",
                    (str(group_id), db.user_id),
                )
                target_ids = [r[0] for r in cur.fetchall()]
                if not target_ids:
                    target_ids = [match_id]
            else:
                # Status guard: only PENDING_REVIEW matches can be approved
                if old_status != "PENDING_REVIEW":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Match is not in PENDING_REVIEW status (current: {old_status})",
                    )
                target_ids = [match_id]

            for mid in target_ids:
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET status = 'USER_APPROVED',
                           user_action = 'approved',
                           actioned_at = NOW(),
                           reviewed_by = %s,
                           reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (user.id, mid, db.user_id),
                )
                # Approving is what settles the pair. A flagged (low-confidence)
                # match leaves its transaction UNMATCHED and in the candidate
                # pool, so the promotion has to happen here —
                # otherwise the user approves a match and nothing changes.
                # Derived from the row just approved, never asserted, and in
                # this same transaction.
                cur.execute(
                    """SELECT transaction_id, document_id
                       FROM reconciliation_matches
                       WHERE id = %s AND user_id = %s""",
                    (mid, db.user_id),
                )
                pair = cur.fetchone()
                if pair:
                    db.resync_statuses_in_cursor(
                        cur, transaction_ids=[pair[0]], document_ids=[pair[1]],
                    )

            # Audit log
            action = "GROUP_APPROVE" if bundle and len(target_ids) > 1 else "APPROVE"
            entity_type = "proof_bundle" if bundle and len(target_ids) > 1 else "match"
            cur.execute(
                """INSERT INTO audit_log
                   (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                   VALUES (%s, %s, %s, %s, %s, 'USER_APPROVED', %s)""",
                (db.user_id, entity_type, match_id, action, old_status, user.id),
            )

    try:
        from server.api.events import send_event
        send_event(user.id, "proofs_updated" if bundle else "match_updated", {
            "match_id": match_id,
            "match_ids": target_ids,
            "action": "approved",
            "group_id": str(group_id) if group_id else None,
        })
    except Exception:
        pass

    return {"status": "ok", "match_id": match_id, "match_ids": target_ids,
            "previous_status": old_status, "new_status": "USER_APPROVED"}


@router.patch("/matches/{match_id}/revert")
async def revert_match_to_review(
    match_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Revert a reconciliation match back to PENDING_REVIEW status.

    Used by the undo action to restore a match after approve/reject.
    Valid from USER_APPROVED, USER_REJECTED, or AUTO_APPROVED states.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, status, transaction_id, document_id, confidence_score
                   FROM reconciliation_matches WHERE id = %s AND user_id = %s""",
                (match_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Match not found")

            old_status = row[1]
            txn_id = row[2]
            doc_id = row[3]
            confidence = float(row[4]) if row[4] is not None else 0.0

            if old_status == "PENDING_REVIEW":
                raise HTTPException(
                    status_code=409,
                    detail="Match is already in PENDING_REVIEW status",
                )

            # Revert match to PENDING_REVIEW
            cur.execute(
                """UPDATE reconciliation_matches
                   SET status = 'PENDING_REVIEW',
                       user_action = 'reset_to_review',
                       actioned_at = NOW(),
                       reviewed_by = %s,
                       reviewed_at = NOW()
                   WHERE id = %s AND user_id = %s""",
                (user.id, match_id, db.user_id),
            )

            # Undo puts the pair back where it was. First re-derive both sides
            # from the surviving rows, then restore the settled state this pair
            # had before the action — but only if the confidence-floor rule
            # says it was settled: a pair below the floor was deliberately left
            # unmatched and in the candidate pool, and undo must not promote it.
            from core.reconciliation import MATCH_CONFIDENCE_FLOOR

            db.resync_statuses_in_cursor(
                cur, transaction_ids=[txn_id], document_ids=[doc_id],
            )
            if confidence >= MATCH_CONFIDENCE_FLOOR:
                if txn_id:
                    cur.execute(
                        """UPDATE transactions SET status = 'MATCHED'
                           WHERE id = %s AND user_id = %s
                             AND status NOT IN ('MATCHED', 'EXCLUDED', 'IGNORED',
                                                'PENDING_REVIEW')""",
                        (txn_id, db.user_id),
                    )
                if doc_id:
                    cur.execute(
                        """UPDATE documents SET status = 'MATCHED', updated_at = NOW()
                           WHERE id = %s AND user_id = %s AND status = 'EXTRACTED'""",
                        (doc_id, db.user_id),
                    )

            # Audit log
            cur.execute(
                """INSERT INTO audit_log
                   (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                   VALUES (%s, 'match', %s, 'RESET_TO_REVIEW', %s, 'PENDING_REVIEW', %s)""",
                (db.user_id, match_id, old_status, user.id),
            )

    try:
        from server.api.events import send_event
        send_event(user.id, "match_updated", {
            "match_id": match_id,
            "action": "reset_to_review",
        })
    except Exception:
        pass

    return {
        "status": "ok",
        "match_id": match_id,
        "previous_status": old_status,
        "new_status": "PENDING_REVIEW",
    }


@router.patch("/matches/{match_id}/reject")
async def reject_match(
    match_id: int,
    bundle: bool = Query(False),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Reject a reconciliation match.

    With ?bundle=true, rejects ALL matches sharing the same group_id.
    Sets status to USER_REJECTED, reverts both the transaction and document
    back to unmatched state, and logs the action.
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Verify match exists
            cur.execute(
                """SELECT id, status, transaction_id, document_id, group_id
                   FROM reconciliation_matches WHERE id = %s AND user_id = %s""",
                (match_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Match not found")

            old_status = row[1]
            txn_id = row[2]
            doc_id = row[3]
            group_id = row[4]

            # Determine targets
            if bundle and group_id:
                cur.execute(
                    """SELECT id, transaction_id, document_id
                       FROM reconciliation_matches
                       WHERE group_id = %s AND user_id = %s AND status != 'USER_REJECTED'""",
                    (str(group_id), db.user_id),
                )
                targets = cur.fetchall()
                target_ids = [r[0] for r in targets]
                target_txn_ids = list({r[1] for r in targets})
                target_doc_ids = [r[2] for r in targets]
            else:
                # Status guard: only PENDING_REVIEW matches can be rejected
                if old_status != "PENDING_REVIEW":
                    raise HTTPException(
                        status_code=409,
                        detail=f"Match is not in PENDING_REVIEW status (current: {old_status})",
                    )
                target_ids = [match_id]
                target_txn_ids = [txn_id] if txn_id else []
                target_doc_ids = [doc_id] if doc_id else []

            # Update all target matches
            for mid in target_ids:
                cur.execute(
                    """UPDATE reconciliation_matches
                       SET status = 'USER_REJECTED',
                           user_action = 'rejected',
                           actioned_at = NOW(),
                           reviewed_by = %s,
                           reviewed_at = NOW()
                       WHERE id = %s AND user_id = %s""",
                    (user.id, mid, db.user_id),
                )

            # Re-derive both sides from the rows that survive the rejection, in
            # this same transaction: the transaction goes back to UNMATCHED only
            # if nothing else proves it (another proof in the bundle, or a
            # cross-statement link), and the receipt is released only if it is
            # not still proving something else.
            db.resync_statuses_in_cursor(
                cur, transaction_ids=target_txn_ids, document_ids=target_doc_ids,
            )

            # Audit log
            action = "GROUP_REJECT" if bundle and len(target_ids) > 1 else "REJECT"
            entity_type = "proof_bundle" if bundle and len(target_ids) > 1 else "match"
            cur.execute(
                """INSERT INTO audit_log
                   (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                   VALUES (%s, %s, %s, %s, %s, 'USER_REJECTED', %s)""",
                (db.user_id, entity_type, match_id, action, old_status, user.id),
            )

    try:
        from server.api.events import send_event
        send_event(user.id, "match_updated", {"match_id": match_id, "action": "rejected"})
    except Exception:
        pass

    return {"status": "ok", "match_id": match_id, "new_status": "USER_REJECTED"}


@router.post("/matches/{match_id}/unmatch")
async def unmatch(
    match_id: int,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Unmatch a previously approved match.

    Reverts both the transaction and document back to unmatched state.
    The match record is preserved for audit trail (status set to UNMATCHED).
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            # Verify match exists
            cur.execute(
                """SELECT id, status, transaction_id, document_id
                   FROM reconciliation_matches WHERE id = %s AND user_id = %s""",
                (match_id, db.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Match not found")

            old_status = row[1]
            txn_id = row[2]
            doc_id = row[3]

            # Status guard: cannot unmatch an already-rejected match
            if old_status == "USER_REJECTED":
                raise HTTPException(
                    status_code=400,
                    detail="Match is already rejected",
                )

            # Update match status (use USER_REJECTED — CHECK constraint only allows
            # AUTO_APPROVED, PENDING_REVIEW, USER_APPROVED, USER_REJECTED)
            cur.execute(
                """UPDATE reconciliation_matches
                   SET status = 'USER_REJECTED',
                       user_action = 'unmatched',
                       actioned_at = NOW(),
                       reviewed_by = %s,
                       reviewed_at = NOW()
                   WHERE id = %s AND user_id = %s""",
                (user.id, match_id, db.user_id),
            )

            # Re-derive both sides from what survives — the transaction may
            # still hold another proof in the bundle, or a cross-statement
            # link, and the receipt may still prove a second transaction.
            db.resync_statuses_in_cursor(
                cur, transaction_ids=[txn_id], document_ids=[doc_id],
            )

            # Audit log
            cur.execute(
                """INSERT INTO audit_log
                   (user_id, entity_type, entity_id, action, old_value, new_value, performed_by)
                   VALUES (%s, 'match', %s, 'UNMATCH', %s, 'USER_REJECTED', %s)""",
                (db.user_id, match_id, old_status, user.id),
            )

    try:
        from server.api.events import send_event
        send_event(user.id, "match_updated", {"match_id": match_id, "action": "unmatched"})
    except Exception:
        pass

    return {"status": "ok", "match_id": match_id, "new_status": "USER_REJECTED"}


# ---------------------------------------------------------------------------
# Manual matching
# ---------------------------------------------------------------------------

class ManualMatchRequest(BaseModel):
    transaction_id: int
    document_id: int | None = None         # legacy singular (still accepted)
    document_ids: list[int] | None = None  # new multi-proof field
    # Split-payment / many-to-many: when true, allow matching a document that is
    # already matched to other transactions (e.g. one receipt covering multiple
    # transactions). Stored as match_type='ONE_TO_MANY'.
    allow_split: bool = False


class ReassignMatchRequest(BaseModel):
    new_document_id: int


def _manual_match_explanation(
    txn_amount,
    txn_currency: str,
    txn_date,
    doc_total,
    doc_currency: str | None,
    doc_date,
    doc_vendor: str | None,
    amt_score: float,
    dt_score: float,
    vnd_score: float,
    include_date: bool = True,
    doc_fields: dict | None = None,
) -> str | None:
    """Sentence stored on a manual match, built from the canonical amount rule.

    The amount clause comes from ``core.amount_match.compare_amounts`` — the
    same rule the auto matcher and the review panel use — and never calls a
    cross-currency pair exact. The manual sub-score cannot be used for this: it
    compares magnitudes with the currencies ignored, so it would report an exact
    match between a CAD row and a USD receipt of the same magnitude.

    ``doc_fields`` is the document row, so the receipt's currency is resolved
    the same way the scorer resolved it (core/document_currency.py) and the
    sentence says which of the three it was — stated, inferred from a Canadian
    tax line, or never stated at all.
    """
    from core.amount_match import compare_amounts
    from core.document_currency import resolve_document_currency

    parts: list[str] = []
    doc_code, doc_source = resolve_document_currency(
        doc_fields if doc_fields is not None else {"currency": doc_currency}
    )
    comparison = compare_amounts(
        txn_amount, txn_currency, doc_total, doc_code,
        doc_currency_source=doc_source,
    )
    parts.append(comparison.phrase())

    if include_date and dt_score >= 0.9:
        if dt_score == 1.0:
            parts.append("Same day")
        else:
            gap = abs((txn_date - doc_date).days) if doc_date else None
            if gap is None:
                parts.append("Date close")
            else:
                parts.append(f"Date {gap} day{'' if gap == 1 else 's'} apart")
    if vnd_score >= 0.5:
        parts.append(f"Vendor match ({doc_vendor})")
    return ". ".join(parts) + "." if parts else None


@router.post("/matches/manual")
async def create_manual_match(
    body: ManualMatchRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Create a manual match between a transaction and a document.

    Supports both singular document_id (backward compat) and document_ids (multi-proof).
    """
    from datetime import date as date_type
    from decimal import Decimal as D

    from core.document_currency import currency_fields
    from core.reconciliation import score_manual_match

    # Resolve effective document IDs
    if body.document_ids:
        effective_doc_ids = body.document_ids
    elif body.document_id is not None:
        effective_doc_ids = [body.document_id]
    else:
        raise HTTPException(400, "Provide either document_id or document_ids")

    if len(effective_doc_ids) > 5:
        raise HTTPException(400, "Maximum 5 documents per bundle")

    # Check if reconciliation is running
    progress = _reconciliation_progress.get(user.id)
    if progress and progress.get("status") == "running":
        raise HTTPException(409, "A reconciliation is currently running. Please wait.")

    with db._conn() as conn:
        with conn.cursor() as cur:
            # Verify transaction
            cur.execute(
                """SELECT id, status, amount, currency, date_posted, description
                   FROM transactions WHERE id = %s AND user_id = %s""",
                (body.transaction_id, db.user_id),
            )
            txn = cur.fetchone()
            if not txn:
                raise HTTPException(404, "Transaction not found")
            if txn[1] == "MATCHED":
                raise HTTPException(409, "Transaction is already matched. Unmatch it first or use reassign.")

    txn_amount = D(str(txn[2])) if txn[2] is not None else D("0")
    txn_currency = txn[3] or "CAD"
    txn_date_raw = txn[4]
    txn_date = txn_date_raw if isinstance(txn_date_raw, date_type) else date_type.fromisoformat(str(txn_date_raw))
    txn_description = txn[5] or ""

    # Single-doc path (backward compat)
    if len(effective_doc_ids) == 1:
        doc_id = effective_doc_ids[0]
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, status, total, currency, document_date, vendor,
                              currency_source, subtotal, tax_gst, tax_hst,
                              tax_pst, tax_other
                       FROM documents WHERE id = %s AND user_id = %s""",
                    (doc_id, db.user_id),
                )
                doc = cur.fetchone()
                if not doc:
                    raise HTTPException(404, "Document not found")
                # When allow_split is true, treat this as a split-payment link
                # (one receipt → multiple transactions). The schema permits this
                # via match_type='ONE_TO_MANY'.
                if doc[1] == "MATCHED" and not body.allow_split:
                    raise HTTPException(409, "This proof of transaction is already matched to another transaction. Link it as a split payment if it covers both.")
                # Guard against linking the same (txn, doc) pair twice.
                if body.allow_split:
                    cur.execute(
                        """SELECT 1 FROM reconciliation_matches
                           WHERE user_id = %s AND transaction_id = %s AND document_id = %s
                             AND status IN ('AUTO_APPROVED','USER_APPROVED','PENDING_REVIEW')
                           LIMIT 1""",
                        (db.user_id, body.transaction_id, doc_id),
                    )
                    if cur.fetchone():
                        raise HTTPException(409, "This receipt is already linked to this transaction.")

        doc_total = D(str(doc[2])) if doc[2] is not None else None
        doc_currency = doc[3]
        # currency_source + the tax columns: what resolves a receipt that
        # states no currency of its own (core/document_currency.py).
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

        explanation = _manual_match_explanation(
            txn_amount, txn_currency, txn_date, doc_total, doc_currency,
            doc_date, doc_vendor, amt_score, dt_score, vnd_score,
            doc_fields=doc_fields,
        )

        match_type = "ONE_TO_MANY" if body.allow_split else "ONE_TO_ONE"
        try:
            result = db.create_manual_match(
                body.transaction_id, doc_id,
                confidence, amt_score, dt_score, vnd_score,
                explanation,
                match_type=match_type,
            )
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise HTTPException(409, "This proof of transaction is already matched; refresh to see the current state.")
            raise

        try:
            from server.api.events import send_event
            send_event(user.id, "match_updated", {
                "match_id": result["match_id"], "action": "manual_match"
            })
        except Exception:
            pass

        return {"status": "ok", **result, "explanation": explanation}

    # Multi-doc path: delegate to proofs endpoint logic
    import uuid
    group_id = str(uuid.uuid4())
    matches = []

    for doc_id in effective_doc_ids:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, status, total, currency, document_date, vendor,
                              currency_source, subtotal, tax_gst, tax_hst,
                              tax_pst, tax_other
                       FROM documents WHERE id = %s AND user_id = %s""",
                    (doc_id, db.user_id),
                )
                doc = cur.fetchone()
                if not doc:
                    raise HTTPException(404, f"Document {doc_id} not found")
                if doc[1] == "MATCHED":
                    raise HTTPException(409, f"Document {doc_id} is already matched to another transaction.")

        doc_total = D(str(doc[2])) if doc[2] is not None else None
        doc_currency = doc[3]
        # currency_source + the tax columns: what resolves a receipt that
        # states no currency of its own (core/document_currency.py).
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

        explanation = _manual_match_explanation(
            txn_amount, txn_currency, txn_date, doc_total, doc_currency,
            doc_date, doc_vendor, amt_score, dt_score, vnd_score,
            include_date=False, doc_fields=doc_fields,
        )

        result = db.add_proof_to_transaction(
            txn_id=body.transaction_id,
            doc_id=doc_id,
            match_type="MANY_TO_ONE",
            confidence_score=confidence,
            amount_score=amt_score,
            date_score=dt_score,
            vendor_score=vnd_score,
            group_id=group_id,
            covered_amount=str(doc_total) if doc_total is not None else None,
            explanation=explanation,
            match_source="MANUAL",
        )
        matches.append({
            "match_id": result["proof_id"],
            "document_id": doc_id,
            "confidence_score": round(confidence, 4),
            "explanation": explanation,
        })
        group_id = result["group_id"]

    try:
        from server.api.events import send_event
        send_event(user.id, "proofs_updated", {
            "transaction_id": body.transaction_id,
            "action": "manual_match",
            "proof_count": len(matches),
            "group_id": group_id,
        })
    except Exception:
        pass

    return {
        "status": "ok",
        "transaction_id": body.transaction_id,
        "matches": matches,
        "group_id": group_id,
    }


@router.post("/matches/{match_id}/reassign")
async def reassign_match(
    match_id: int,
    body: ReassignMatchRequest,
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Reassign a match to a different document (atomic unmatch + rematch)."""
    from datetime import date as date_type
    from decimal import Decimal as D

    from core.document_currency import currency_fields
    from core.reconciliation import score_manual_match

    # Check if reconciliation is running
    progress = _reconciliation_progress.get(user.id)
    if progress and progress.get("status") == "running":
        raise HTTPException(409, "A reconciliation is currently running. Please wait.")

    with db._conn() as conn:
        with conn.cursor() as cur:
            # Verify old match
            cur.execute(
                """SELECT rm.transaction_id, rm.status, t.amount, t.currency, t.date_posted, t.description
                   FROM reconciliation_matches rm
                   JOIN transactions t ON rm.transaction_id = t.id
                   WHERE rm.id = %s AND rm.user_id = %s""",
                (match_id, db.user_id),
            )
            match_row = cur.fetchone()
            if not match_row:
                raise HTTPException(404, "Match not found")
            if match_row[1] == "USER_REJECTED":
                raise HTTPException(409, "Match is already rejected")

            # Verify new document
            cur.execute(
                """SELECT id, status, total, currency, document_date, vendor,
                          currency_source, subtotal, tax_gst, tax_hst,
                          tax_pst, tax_other
                   FROM documents WHERE id = %s AND user_id = %s""",
                (body.new_document_id, db.user_id),
            )
            doc = cur.fetchone()
            if not doc:
                raise HTTPException(404, "Document not found")
            if doc[1] == "MATCHED":
                raise HTTPException(409, "This receipt is already matched to another transaction.")

    # Score
    txn_amount = D(str(match_row[2])) if match_row[2] is not None else D("0")
    txn_currency = match_row[3] or "CAD"
    txn_date_raw = match_row[4]
    txn_date = txn_date_raw if isinstance(txn_date_raw, date_type) else date_type.fromisoformat(str(txn_date_raw))
    txn_description = match_row[5] or ""

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

    try:
        result = db.reassign_match(
            match_id, body.new_document_id,
            confidence, amt_score, dt_score, vnd_score,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(409, "This proof of transaction is already matched; refresh to see the current state.")
        raise

    try:
        from server.api.events import send_event
        send_event(user.id, "match_updated", {
            "match_id": result["new_match_id"], "action": "reassigned"
        })
    except Exception:
        pass

    return {
        "status": "ok",
        **result,
        "confidence_score": confidence,
        "amount_score": amt_score,
        "date_score": dt_score,
        "vendor_score": vnd_score,
    }


@router.get("/audit-log")
async def reconciliation_audit_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    entity_type: str | None = None,
    entity_id: int | None = None,
    start_date: str | None = Query(None, description="ISO date YYYY-MM-DD inclusive"),
    end_date: str | None = Query(None, description="ISO date YYYY-MM-DD inclusive"),
    user: AuthUser = Depends(get_current_user),
    db: DatabasePg = Depends(get_db),
):
    """Paginated audit log for reconciliation actions.

    Supports filtering by entity_type ('match', 'document', 'transaction'),
    entity_id for drilling into a specific record's history,
    and date range via start_date / end_date (YYYY-MM-DD, inclusive).
    """
    with db._conn() as conn:
        with conn.cursor() as cur:
            where_clauses = ["user_id = %s"]
            params: list = [db.user_id]

            if entity_type:
                where_clauses.append("entity_type = %s")
                params.append(entity_type)

            if entity_id is not None:
                where_clauses.append("entity_id = %s")
                params.append(entity_id)

            if start_date:
                where_clauses.append("created_at >= %s::date")
                params.append(start_date)

            if end_date:
                where_clauses.append("created_at < (%s::date + interval '1 day')")
                params.append(end_date)

            where_sql = " AND ".join(where_clauses)

            # Count
            cur.execute("SELECT COUNT(*) FROM audit_log WHERE " + where_sql, params)
            total = cur.fetchone()[0]

            # Paginated results
            offset = (page - 1) * per_page
            params.extend([per_page, offset])
            cur.execute(
                "SELECT id, entity_type, entity_id, action,"
                "       old_value, new_value, performed_by, created_at"
                " FROM audit_log"
                " WHERE " + where_sql +
                " ORDER BY created_at DESC"
                " LIMIT %s OFFSET %s",
                params,
            )

            entries = []
            for row in cur.fetchall():
                entries.append({
                    "id": row[0],
                    "entity_type": row[1],
                    "entity_id": row[2],
                    "action": row[3],
                    "old_value": row[4],
                    "new_value": row[5],
                    "performed_by": row[6],
                    "created_at": str(row[7]) if row[7] else None,
                })

            return {
                "entries": entries,
                "total": total,
                "page": page,
                "per_page": per_page,
            }


@router.get("/audit-log/export")
async def audit_log_export_csv(
    entity_type: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    user: AuthUser = Depends(get_current_user),
    _verified: AuthUser = Depends(require_verified_email),
    db: DatabasePg = Depends(get_db),
):
    """Export the full audit log as a CSV file.

    Supports the same filters as the paginated audit-log endpoint
    (entity_type, start_date, end_date) but returns ALL matching rows
    (up to a 50 000-row safety cap) as a downloadable CSV.
    """
    SAFETY_CAP = 50_000

    with db._conn() as conn:
        with conn.cursor() as cur:
            where_clauses = ["user_id = %s"]
            params: list = [db.user_id]

            if entity_type:
                where_clauses.append("entity_type = %s")
                params.append(entity_type)

            if start_date:
                where_clauses.append("created_at >= %s::date")
                params.append(start_date)

            if end_date:
                where_clauses.append("created_at < (%s::date + interval '1 day')")
                params.append(end_date)

            where_sql = " AND ".join(where_clauses)

            # Count first to detect truncation
            cur.execute("SELECT COUNT(*) FROM audit_log WHERE " + where_sql, params)
            total = cur.fetchone()[0]
            truncated = total > SAFETY_CAP

            # Fetch rows (with safety cap)
            params.append(SAFETY_CAP)
            cur.execute(
                "SELECT id, entity_type, entity_id, action,"
                "       old_value, new_value, performed_by, created_at"
                " FROM audit_log"
                " WHERE " + where_sql +
                " ORDER BY created_at DESC"
                " LIMIT %s",
                params,
            )
            rows = cur.fetchall()

    # Build CSV in memory
    output = io.StringIO()
    writer = SafeCsvWriter(csv.writer(output))
    writer.writerow([
        "id", "entity_type", "entity_id", "action",
        "old_value", "new_value", "performed_by", "created_at",
    ])
    for row in rows:
        writer.writerow([
            row[0],
            row[1],
            row[2],
            row[3],
            row[4] if row[4] is not None else "",
            row[5] if row[5] is not None else "",
            row[6] if row[6] is not None else "",
            str(row[7]) if row[7] else "",
        ])

    row_count = len(rows)

    # Record the export itself in the audit log
    try:
        db.insert_audit_log(
            entity_type="export",
            entity_id=0,
            action="EXPORT_CSV",
            performed_by=user.id,
            new_value={"type": "audit_log", "row_count": row_count},
        )
    except Exception:
        pass  # Best-effort — don't fail the export

    today = date_type.today().isoformat()
    headers = {
        "Content-Disposition": f'attachment; filename="audit-log-{today}.csv"',
    }
    if truncated:
        headers["X-Truncated"] = "true"

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers=headers,
    )
