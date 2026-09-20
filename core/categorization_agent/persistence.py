"""Phase 7 — write all finalized decisions to the database.

One database transaction that upserts the taxonomy and vendor cache,
bulk-updates every in-scope transaction, and inserts the audit row.
User-confirmed categorizations are never touched — the SQL WHERE clause
in ``update_transaction_categorizations`` enforces the hard lock.
"""

from __future__ import annotations

import logging
from datetime import datetime

from config.ised_categories import coerce_to_valid

from .context import (
    CategorizationContext,
    TxnRow,
    VendorCluster,
    VendorDecision,
)
from .rule_prepass import match_category

logger = logging.getLogger(__name__)

# Phrases in a user note that mean the USER themselves does not know what the
# transaction is. A user's own categorization wins: the engine must respect it and leave
# the row Uncategorized rather than guess. Conservative + identity-focused so it
# does not fire on "not sure if deductible" style notes.
_UNCERTAINTY_NOTE_PHRASES: tuple[str, ...] = (
    "uncertain what", "not sure what", "unsure what", "don't know what",
    "dont know what", "no idea what", "no idea", "unclear what", "unknown",
    "what is this", "what's this", "not sure what this is",
)

# Phrases in a user note that unambiguously mean the transaction is a refund /
# reversal / chargeback -> Refund/Credit, even when a deterministic rule pattern
# would otherwise call it revenue or an expense (e.g. a DirectDeposit noted
# "purchase refunded"). Conservative so it does not fire on stray
# mentions ("no refund policy" is not something a per-txn note usually says).
_REFUND_NOTE_PHRASES: tuple[str, ...] = (
    "refund", "chargeback", "charge back", "reimburse", "credit memo",
    "money came back", "money back", "was returned",
)


def _note_expresses_uncertainty(note: str | None) -> bool:
    if not note:
        return False
    n = note.lower()
    return any(p in n for p in _UNCERTAINTY_NOTE_PHRASES)


def _note_signals_refund(note: str | None) -> bool:
    if not note:
        return False
    n = note.lower()
    return any(p in n for p in _REFUND_NOTE_PHRASES)


def _note_forced_category(note: str | None, *, allow_uncertainty: bool = True):
    """If a user note UNAMBIGUOUSLY dictates the category, return ``(True, category)``.

    ``category`` is ``None`` for an uncertainty note (-> Uncategorized) or the
    canonical string for a refund note. Otherwise ``(False, None)``.
    ``allow_uncertainty=False`` (rule-locked rows) skips the uncertainty rule so
    a confident deterministic match (e.g. a bank fee) is not blanked by a vague
    "not sure" note — only an explicit refund overrides a rule-locked category.
    """
    if allow_uncertainty and _note_expresses_uncertainty(note):
        return True, None
    if _note_signals_refund(note):
        return True, "Refund/Credit"
    return False, None


def build_transaction_updates(
    context: CategorizationContext,
    clusters: list[VendorCluster],
    decisions: list[VendorDecision],
    refinement_overrides: dict[int, dict],
    rule_locked_ids: set[int],
    txn_lookup: dict[int, TxnRow],
) -> list[dict]:
    """Assemble the full list of txn rows to upsert.

    Each update carries: id, category, category_source, category_confidence,
    category_reasoning, vendor_key. Rows whose id is in ``rule_locked_ids``
    are written with ``category_source='rule'``; vendor decisions win for
    everything else unless a per-txn refinement override is present.
    """
    updates: list[dict] = []

    # Rule-locked rows. Rule outputs are already canonical; coerce_to_valid is a
    # safety net (off-list -> None -> stored NULL / Uncategorized).
    for txn_id in rule_locked_ids:
        txn = txn_lookup.get(txn_id)
        if txn is None:
            continue
        category = coerce_to_valid(match_category(txn.description))
        reasoning = "Matched deterministic rule pattern."
        # An explicit refund note overrides the rule (a "DirectDeposit" noted
        # "...refunded" is a Refund/Credit, not revenue). Uncertainty notes do
        # NOT override a confident rule match.
        forced, forced_cat = _note_forced_category(txn.note, allow_uncertainty=False)
        if forced:
            category = coerce_to_valid(forced_cat)
            reasoning = "User note indicates a refund; overrides the rule category."
        updates.append({
            "id": txn_id,
            "category": category,
            "category_source": "rule",
            "category_confidence": "high",
            "category_reasoning": reasoning,
            "vendor_key": None,
        })

    # Vendor/refinement decisions, expanded to the transaction level.
    decision_by_key = {d.vendor_key: d for d in decisions}
    for cluster in clusters:
        decision = decision_by_key.get(cluster.vendor_key)
        if decision is None:
            continue
        for txn_id in cluster.transaction_ids:
            if txn_id in rule_locked_ids:
                continue  # Rule already wrote this row.
            # An explicit user note dictates the category for an LLM-decided row:
            # a refund note -> Refund/Credit; an "I don't know what this is" note
            # -> Uncategorized (never guess against the user's stated uncertainty).
            txn = txn_lookup.get(txn_id)
            forced, forced_cat = _note_forced_category(txn.note if txn else None)
            if forced:
                updates.append({
                    "id": txn_id,
                    "category": coerce_to_valid(forced_cat),
                    "category_source": "agent",
                    "category_confidence": "low",
                    "category_reasoning": "User note dictates this category.",
                    "vendor_key": cluster.vendor_key,
                })
                continue
            override = refinement_overrides.get(txn_id)
            if override:
                updates.append({
                    "id": txn_id,
                    "category": coerce_to_valid(override.get("category")),
                    "category_source": "agent",
                    "category_confidence": override.get("confidence", "medium"),
                    "category_reasoning": override.get("reasoning"),
                    "vendor_key": cluster.vendor_key,
                })
            else:
                updates.append({
                    "id": txn_id,
                    "category": coerce_to_valid(decision.category_name),
                    "category_source": "agent",
                    "category_confidence": decision.confidence,
                    "category_reasoning": decision.reasoning,
                    "vendor_key": cluster.vendor_key,
                })

    return updates


def persist_run(
    db,
    context: CategorizationContext,
    final_taxonomy: list[dict],
    decisions: list[VendorDecision],
    clusters: list[VendorCluster],
    refinement_overrides: dict[int, dict],
    rule_locked_ids: set[int],
    txn_lookup: dict[int, TxnRow],
    started_at: datetime,
    completed_at: datetime,
    llm_calls: int,
    taxonomy_diff: dict,
    consistency_diff: dict,
    status: str,
    error: str | None = None,
) -> tuple[int, int]:
    """Persist taxonomy, vendor cache, transaction updates, and run audit.

    Returns ``(run_id, rows_updated)``. This runs on a single DatabasePg
    instance which opens one pooled connection per statement — psycopg2
    offers no cross-method transaction here, so each statement commits
    independently. That is safe because user-confirmed rows are never
    overwritten and all writes are upserts or plain UPDATEs.
    """
    # Inject every rule-prepass category actually used by rule-locked rows
    # into the final taxonomy. Without this, the LLM-synthesized taxonomy
    # may not contain names like "Equipment" or "Bank Fee" — and the UI
    # dropdown (which is built from user_categories) would then have no
    # matching option for rule-locked rows, visually falling back to the
    # wrong category even though the database value is correct.
    rule_categories_used: set[str] = set()
    for txn_id in rule_locked_ids:
        txn = txn_lookup.get(txn_id)
        if txn is None:
            continue
        cat = coerce_to_valid(match_category(txn.description))
        if cat:
            rule_categories_used.add(cat)

    existing_names = {(c.get("name") or "").strip() for c in final_taxonomy}
    injected: list[str] = []
    for cat_name in sorted(rule_categories_used):
        if cat_name and cat_name not in existing_names:
            # Defensive no-op: every rule output is a canonical name already
            # seeded by the fixed taxonomy, so this branch never fires. Keep it
            # inert (no fabricated definition/kind) as a safety net.
            final_taxonomy.append({
                "name": cat_name,
                "definition": None,
                "umbrella_for": None,
                "example_vendors": [],
                "kind": None,
                "is_system": True,
            })
            existing_names.add(cat_name)
            injected.append(cat_name)
    logger.info(
        "persist_run: rule_locked=%d rule_cats_used=%s injected=%s existing_count=%d",
        len(rule_locked_ids), sorted(rule_categories_used), injected, len(existing_names),
    )

    # 1) Taxonomy.
    if final_taxonomy:
        db.upsert_user_categories(final_taxonomy)

    # 2) Vendor cache.
    vendor_cache_entries = []
    for cluster in clusters:
        decision = next((d for d in decisions if d.vendor_key == cluster.vendor_key), None)
        if decision is None:
            continue
        vendor_cache_entries.append({
            "vendor_key": cluster.vendor_key,
            "category_name": decision.category_name,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "sample_desc": cluster.sample_descriptions[0] if cluster.sample_descriptions else None,
        })
    if vendor_cache_entries:
        db.upsert_vendor_cache(vendor_cache_entries)

    # 3) Transaction updates.
    updates = build_transaction_updates(
        context=context,
        clusters=clusters,
        decisions=decisions,
        refinement_overrides=refinement_overrides,
        rule_locked_ids=rule_locked_ids,
        txn_lookup=txn_lookup,
    )
    rows_updated = db.update_transaction_categorizations(updates)

    # 4) Audit row.
    summary = {
        "phases": {
            "rule_locked": len(rule_locked_ids),
            "vendors_decided": len(decisions),
            "txns_refined": len(refinement_overrides),
            "categories_final": len(final_taxonomy),
        },
        "taxonomy_diff": taxonomy_diff,
        "consistency_diff": consistency_diff,
    }
    run_id = db.insert_categorization_run({
        "started_at": started_at,
        "completed_at": completed_at,
        "scope": context.scope,
        "total": len(context.transactions),
        "categorized": rows_updated,
        "rule_locked": len(rule_locked_ids),
        "llm_calls": llm_calls,
        "status": status,
        "error": error,
        "summary": summary,
    })
    return run_id, rows_updated
