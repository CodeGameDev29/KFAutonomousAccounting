"""Phase 5 — per-transaction refinement for mixed-use vendors.

Runs only for transactions in vendor clusters that Phase 4 marked
``needs_refinement=True``. Each sub-batch of ~10 transactions shares the
same vendor context — sibling descriptions and the full taxonomy — so
the LLM can decide each txn's specific purpose.

Nothing in a batch depends on another batch's answer, so every batch across
every mixed-use vendor goes out together, bounded by ``MAX_CONCURRENT_CALLS``.
The overrides are applied in batch order afterwards, so the result does not
depend on which call finished first — and each batch's reply may only touch the
transaction ids that batch asked about. A reply that names some other batch's id is dropped: with
several batches in flight at once, an id echoed back from the wrong context is
a category written onto a transaction the model never saw.
"""

from __future__ import annotations

import json
import logging

from config.ised_categories import ASSIGNABLE_NAME_SET, coerce_to_valid

from .concurrency import run_bounded
from .context import TxnRow, VendorCluster
from .llm_shapes import excerpt, unwrap_item_list
from .prompts import CLOSED_LIST_RULES, REFINEMENT
from .vendor_clustering import _summarize_proof_line_items

logger = logging.getLogger(__name__)

REFINE_BATCH_SIZE = 10


def _serialize_txn_for_refinement(t: TxnRow) -> dict:
    """Serialize a single txn for the refinement prompt.

    Includes matched-receipt proof (vendor, total, line items, invoice)
    when available so the LLM can categorize from WHAT WAS BOUGHT rather
    than from the payment-processor wrapper text.
    """
    base = {
        "id": t.id,
        "date": t.date_posted.isoformat(),
        "amount": round(t.amount, 2),
        "currency": t.currency,
        "description": t.description,
    }
    # PRIMARY SIGNAL — the user's own note on this transaction (authoritative).
    if (t.note or "").strip():
        base["user_note"] = t.note.strip()
    if t.proof:
        base["matched_proof"] = {
            "source": t.proof.get("source"),
            "link_depth": t.proof.get("link_depth", 0),
            "proof_vendor": (t.proof.get("vendor") or "").strip() or None,
            "proof_total": t.proof.get("total"),
            "proof_currency": t.proof.get("currency"),
            "proof_invoice_number": t.proof.get("invoice_number"),
            "proof_line_items": _summarize_proof_line_items(t.proof.get("line_items_json")),
        }
    return base


def refine_transactions(
    mixed_clusters: list[VendorCluster],
    txn_lookup: dict[int, TxnRow],
    taxonomy: list[dict],
    business_profile_text: str,
    llm_call: callable,
    budget: dict | None = None,
) -> dict[int, dict]:
    """Return a per-txn override map: ``{txn_id: {category, confidence, reasoning}}``.

    Transactions not present in the return map keep their vendor-level
    assignment from Phase 4. Fallback-safe: if a batch fails, those txns
    are simply absent from the map.
    """
    if not mixed_clusters:
        return {}

    # Only the ASSIGNABLE names are offered to the LLM; journal-only categories
    # are never assignable to a transaction, so they are withheld.
    serialized_taxonomy = [
        {
            "name": c.get("name"),
            "definition": c.get("definition"),
            "kind": c.get("kind"),
        }
        for c in taxonomy
        if c.get("name") in ASSIGNABLE_NAME_SET
    ]

    overrides: dict[int, dict] = {}

    # ── Build every batch's prompt first, on this thread ──────────────────
    #
    # The budget is counted here, in order, so the tally is exact however the
    # calls interleave — and a batch that would exceed it is simply never sent.
    # (vendor_key, prompt, the ids this prompt asked about)
    prompts: list[tuple[str, str, frozenset[int]]] = []
    exhausted = False
    for cluster in mixed_clusters:
        if exhausted:
            break
        cluster_txns = [txn_lookup[tid] for tid in cluster.transaction_ids if tid in txn_lookup]
        if not cluster_txns:
            continue

        sibling_samples = "\n".join(
            f"- {d}" for d in cluster.sample_descriptions[:5]
        ) or "(none)"

        for start_i in range(0, len(cluster_txns), REFINE_BATCH_SIZE):
            batch = cluster_txns[start_i:start_i + REFINE_BATCH_SIZE]

            if budget is not None and budget.get("used", 0) >= budget.get("max", 1_000_000):
                logger.warning("LLM budget exhausted during refinement; skipping remaining batches")
                exhausted = True
                break

            prompts.append((
                cluster.vendor_key,
                REFINEMENT.format(
                    closed_list_rules=CLOSED_LIST_RULES,
                    business_profile=business_profile_text,
                    taxonomy_json=json.dumps(serialized_taxonomy, ensure_ascii=False, indent=2),
                    vendor_key=cluster.vendor_key,
                    count=cluster.count,
                    date_range=(
                        f"{cluster.date_range[0].isoformat()}"
                        f"..{cluster.date_range[1].isoformat()}"
                    ),
                    sibling_samples=sibling_samples,
                    transactions_json=json.dumps(
                        [_serialize_txn_for_refinement(t) for t in batch],
                        ensure_ascii=False,
                        indent=2,
                    ),
                ),
                frozenset(t.id for t in batch),
            ))

            if budget is not None:
                budget["used"] = budget.get("used", 0) + 1

    def _refine_batch(
        item: tuple[str, str, frozenset[int]],
    ) -> tuple[list[dict], frozenset[int], str]:
        """Return ``(refinement dicts, the ids asked about, a log excerpt)``."""
        vendor_key, prompt, batch_ids = item
        try:
            response = llm_call(prompt)
        except Exception as exc:
            logger.warning("Refinement batch for %s failed: %s", vendor_key, exc)
            return [], batch_ids, f"call raised {type(exc).__name__}: {exc}"
        return unwrap_item_list(response, "id"), batch_ids, excerpt(response)

    for response, batch_ids, raw_excerpt in run_bounded(prompts, _refine_batch):
        stray: list[int] = []
        for item in response:
            if not isinstance(item, dict):
                continue
            try:
                txn_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            # An id this batch did not ask about is not this batch's to write.
            # Batches run concurrently across every mixed-use vendor, so an
            # echoed-back id from elsewhere would otherwise silently overwrite a
            # transaction the model never saw the context for.
            if txn_id not in batch_ids:
                stray.append(txn_id)
                continue
            # Closed-set gate: off-list / journal-only -> None (-> stored NULL).
            category = coerce_to_valid((item.get("category") or "").strip())
            confidence = item.get("confidence") or "medium"
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"
            reasoning = (item.get("reasoning") or "").strip() or "(refined from mixed-use vendor)"
            overrides[txn_id] = {
                "category": category,
                "confidence": confidence,
                "reasoning": reasoning,
            }
        if stray:
            logger.warning(
                "Refinement reply named %d transaction id(s) outside its own "
                "batch of %d and they were dropped: %s. raw=%s",
                len(stray), len(batch_ids), stray, raw_excerpt,
            )

    return overrides
