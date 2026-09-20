"""Phase 4 — vendor-level categorization with rolling cross-group state.

For each vendor cluster, pick a category from the taxonomy. Vendors are sent in
groups of 20, and a group's prompt carries a ``decisions_so_far`` snapshot so
the model can keep cross-group consistency (same vendor family → same
category).

Groups run **a wave at a time** rather than one at a time: up to
``MAX_CONCURRENT_CALLS`` of them are in flight together, and the next wave sees
every decision the waves before it made. A set of books small enough to fit in
one wave is the whole phase in a single round trip instead of one round trip per
20 vendors; the groups that share a wave see a narrower rolling context than
they would one at a time. Phase 6's self-consistency pass reviews every decision
together afterwards, and the taxonomy is a fixed closed list, so nothing about a
category's *existence* depends on what another group decided.

A group's reply is read back through :mod:`core.categorization_agent.llm_shapes`
rather than by testing ``isinstance(response, list)``. The transport hands back
a parsed JSON *object* (the endpoint is called with
``response_format={"type": "json_object"}``), so a list test would reject every
reply and report each vendor as "LLM did not return a decision for this vendor."
"""

from __future__ import annotations

import json
import logging

from config.ised_categories import ASSIGNABLE_NAME_SET, coerce_to_valid

from .concurrency import MAX_CONCURRENT_CALLS, run_bounded
from .context import VendorCluster, VendorDecision
from .llm_shapes import excerpt, normalize_key, unwrap_item_list
from .prompts import CLOSED_LIST_RULES, VENDOR_DECISIONS

logger = logging.getLogger(__name__)

VENDOR_GROUP_SIZE = 20


def _serialize_cluster_for_decision(cluster: VendorCluster) -> dict:
    return {
        "vendor_key": cluster.vendor_key,
        "count": cluster.count,
        "total_by_currency": {k: round(v, 2) for k, v in cluster.total_by_currency.items()},
        "date_range": f"{cluster.date_range[0].isoformat()}..{cluster.date_range[1].isoformat()}",
        "sample_descriptions": cluster.sample_descriptions[:5],
        # PRIMARY SIGNAL — notes the USER wrote on these transactions. Authoritative:
        # the user knows their own transactions. Read + comprehend before deciding.
        "user_notes": cluster.sample_notes[:5],
        # PRIMARY SIGNAL — matched-receipt proof (direct or via link chain).
        # When non-empty, these describe what the txns are: the merchant,
        # totals, and line items from reconciled receipts.
        # The bank description is often just a payment-processor wrapper
        # (PayPal / Wise / FX) and must NOT override the proof.
        "proof_coverage": cluster.proof_coverage,
        "proof_examples": cluster.proof_examples[:5],
    }


def _serialize_taxonomy(taxonomy: list[dict]) -> list[dict]:
    # Only the ASSIGNABLE names are offered to the LLM; journal-only categories
    # are never assignable to a transaction, so they are withheld.
    return [
        {
            "name": c.get("name"),
            "definition": c.get("definition"),
            "kind": c.get("kind"),
        }
        for c in taxonomy
        if c.get("name") in ASSIGNABLE_NAME_SET
    ]


def _align_decisions(
    raw_items: list[dict],
    batch: list[VendorCluster],
    group_num: int,
    raw_excerpt: str,
) -> dict[str, dict]:
    """Map each cluster in ``batch`` to the decision the model meant for it.

    Keys are matched after normalization, so a model that title-cases or
    hyphenates the key it was sent ("Bluepeak Software" for
    "bluepeak_software") still lands on the right cluster. If NOTHING matches
    by key and the reply happens to carry exactly one entry per cluster, the
    entries are taken positionally — the model answered in order and only
    renamed the labels.
    That is a last resort and is logged, because guessing the alignment when
    some keys DID match would be how one vendor's decision lands on another's fee.

    Whatever is still unaccounted for is logged at WARNING with the vendor keys
    and a 300-char excerpt of the reply, so the next failure is diagnosable from
    the server error log rather than from a re-run.
    """
    by_norm: dict[str, dict] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        norm = normalize_key(item.get("vendor_key"))
        if norm and norm not in by_norm:
            by_norm[norm] = item

    resolved: dict[str, dict] = {}
    for cluster in batch:
        item = by_norm.get(normalize_key(cluster.vendor_key))
        if item is not None:
            resolved[cluster.vendor_key] = item

    if not resolved and len(raw_items) == len(batch) and raw_items:
        logger.warning(
            "Vendor decision group %d: no vendor_key in the reply matched this "
            "batch; falling back to positional alignment of %d entries. raw=%s",
            group_num, len(batch), raw_excerpt,
        )
        for cluster, item in zip(batch, raw_items):
            resolved[cluster.vendor_key] = item

    missing = [c.vendor_key for c in batch if c.vendor_key not in resolved]
    if missing:
        logger.warning(
            "Vendor decision group %d: no decision for %d/%d vendors %s "
            "(reply carried %d entries). raw=%s",
            group_num, len(missing), len(batch), missing, len(raw_items),
            raw_excerpt,
        )
    return resolved


def decide_vendor_categories(
    clusters: list[VendorCluster],
    taxonomy: list[dict],
    business_profile_text: str,
    seed_decisions: dict[str, str] | None,
    llm_call: callable,
    on_decision: callable | None = None,
    budget: dict | None = None,
) -> list[VendorDecision]:
    """Run the group-of-20 vendor decision loop.

    Returns one ``VendorDecision`` per cluster. Any cluster the LLM fails
    on falls back to category ``"Others"`` with ``needs_refinement=True``
    so Phase 5 can try again per-transaction.

    ``seed_decisions`` is the prior vendor cache — cached entries skip
    the LLM call entirely (idempotent re-runs).

    ``budget`` is an optional shared counter ``{"used": int, "max": int}``;
    when ``used >= max`` the remaining vendors default to "Others".
    """
    valid_category_names = ASSIGNABLE_NAME_SET  # closed list is authoritative

    decisions: list[VendorDecision] = []
    decisions_so_far: dict[str, str] = {}
    serialized_taxonomy = _serialize_taxonomy(taxonomy)

    # Apply cached decisions up front (skip the LLM for repeat vendors).
    # BUT: if the cluster has matched-receipt proof, never trust the cache —
    # a prior run may have decided this cluster from the description alone and
    # flagged it as a payment-processor fee when the matched receipt says
    # otherwise. Forcing a fresh LLM decision with the proof_examples is the
    # whole point of the proof-grounded agent.
    to_decide: list[VendorCluster] = []
    for cluster in clusters:
        cached = (seed_decisions or {}).get(cluster.vendor_key)
        if (
            cached
            and cached in valid_category_names
            and cluster.proof_coverage <= 0.0
        ):
            dec = VendorDecision(
                vendor_key=cluster.vendor_key,
                category_name=cached,
                confidence="high",
                reasoning="Reused from vendor cache (prior run).",
                needs_refinement=False,
            )
            decisions.append(dec)
            decisions_so_far[cluster.vendor_key] = cached
            if on_decision is not None:
                on_decision(dec, len(decisions), len(clusters))
        else:
            to_decide.append(cluster)

    groups = [
        to_decide[i:i + VENDOR_GROUP_SIZE]
        for i in range(0, len(to_decide), VENDOR_GROUP_SIZE)
    ]

    def _build_prompt(batch: list[VendorCluster], context: dict[str, str]) -> str:
        return VENDOR_DECISIONS.format(
            closed_list_rules=CLOSED_LIST_RULES,
            business_profile=business_profile_text,
            taxonomy_json=json.dumps(serialized_taxonomy, ensure_ascii=False, indent=2),
            decisions_so_far_json=json.dumps(context, ensure_ascii=False, indent=2),
            vendors_json=json.dumps(
                [_serialize_cluster_for_decision(c) for c in batch],
                ensure_ascii=False,
                indent=2,
            ),
        )

    def _decide_group(item: tuple[int, list[VendorCluster], str]) -> tuple[list[dict], str]:
        """Return ``(decision dicts, a log-safe excerpt of what came back)``.

        The excerpt travels with the reply so the caller can name the vendors
        it could not find AND show what the model replied, in one WARNING,
        rather than leaving a phase that produced nothing undiagnosable.
        """
        group_num, batch, prompt = item
        try:
            response = llm_call(prompt)
        except Exception as exc:
            logger.warning("Vendor decision group %d failed: %s", group_num, exc)
            return [], f"call raised {type(exc).__name__}: {exc}"
        return unwrap_item_list(response, "vendor_key"), excerpt(response)

    group_num = 0
    exhausted = False
    for wave_start in range(0, len(groups), MAX_CONCURRENT_CALLS):
        wave = groups[wave_start:wave_start + MAX_CONCURRENT_CALLS]
        # Every group in a wave asks the same question of the rolling context;
        # the wave after it sees everything this one decided.
        context_snapshot = dict(decisions_so_far)
        work: list[tuple[int, list[VendorCluster], str | None]] = []
        for batch in wave:
            group_num += 1
            # Budget check: a group past the cap is never sent, and its vendors
            # default to Others for Phase 5 to try per transaction. Counted
            # here, on one thread, before anything is in flight — so the tally
            # is exact however the calls interleave.
            if (
                exhausted
                or (budget is not None
                    and budget.get("used", 0) >= budget.get("max", 1_000_000))
            ):
                exhausted = True
                logger.warning(
                    "LLM budget exhausted mid-Phase-4; defaulting %d vendors to Others",
                    len(batch),
                )
                work.append((group_num, batch, None))
                continue
            if budget is not None:
                budget["used"] = budget.get("used", 0) + 1
            work.append((group_num, batch, _build_prompt(batch, context_snapshot)))

        askable = [item for item in work if item[2] is not None]
        # One wave, several calls in flight; the replies are then read back in
        # group order, so `decisions` is assembled in group order however the
        # calls interleave.
        replies = dict(zip(
            (item[0] for item in askable), run_bounded(askable, _decide_group),
        ))

        for num, batch, prompt in work:
            if prompt is None:
                for cluster in batch:
                    dec = VendorDecision(
                        vendor_key=cluster.vendor_key,
                        category_name=None,
                        confidence="low",
                        reasoning="LLM budget exhausted during run.",
                        needs_refinement=True,
                    )
                    decisions.append(dec)
                    decisions_so_far[cluster.vendor_key] = None
                    if on_decision is not None:
                        on_decision(dec, len(decisions), len(clusters))
                continue
            raw_items, raw_excerpt = replies.get(num) or ([], "(no reply)")
            by_key = _align_decisions(raw_items, batch, num, raw_excerpt)

            for cluster in batch:
                item = by_key.get(cluster.vendor_key)
                if not item:
                    dec = VendorDecision(
                        vendor_key=cluster.vendor_key,
                        category_name=None,
                        confidence="low",
                        reasoning="LLM did not return a decision for this vendor.",
                        needs_refinement=True,
                    )
                else:
                    # Closed-set gate: off-list / journal-only -> None (-> stored NULL).
                    category_name = coerce_to_valid((item.get("category") or "").strip())
                    confidence = item.get("confidence") or "medium"
                    if confidence not in ("high", "medium", "low"):
                        confidence = "medium"
                    reasoning = (item.get("reasoning") or "").strip() or "(no reasoning provided)"
                    needs_refinement = bool(item.get("needs_refinement")) or confidence == "low"
                    dec = VendorDecision(
                        vendor_key=cluster.vendor_key,
                        category_name=category_name,
                        confidence=confidence,
                        reasoning=reasoning,
                        needs_refinement=needs_refinement,
                    )
                decisions.append(dec)
                decisions_so_far[cluster.vendor_key] = dec.category_name
                if on_decision is not None:
                    on_decision(dec, len(decisions), len(clusters))

    return decisions
