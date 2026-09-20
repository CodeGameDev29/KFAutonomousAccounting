"""Phase 6 — self-consistency check.

One final LLM call that reviews the whole run's decisions grouped by
category and flags vendor decisions that contradict their category's
scope. The taxonomy is closed, so the only honoured output is a
vendor-level reassignment; it is applied in memory before Phase 7 writes
anything.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict

from config.ised_categories import ASSIGNABLE_NAME_SET, coerce_to_valid

from .context import VendorDecision
from .prompts import SELF_CONSISTENCY

logger = logging.getLogger(__name__)


def run_consistency_check(
    taxonomy: list[dict],
    decisions: list[VendorDecision],
    llm_call: callable,
    budget: dict | None = None,
) -> tuple[list[dict], list[VendorDecision], dict]:
    """Return (updated_taxonomy, updated_decisions, consistency_diff).

    Fallback-safe: if the LLM fails, return the inputs unchanged with an
    empty diff so the caller can still proceed to persistence.
    """
    if not decisions:
        return taxonomy, decisions, {}

    if budget is not None and budget.get("used", 0) >= budget.get("max", 1_000_000):
        logger.warning("LLM budget exhausted before consistency check; skipping")
        return taxonomy, decisions, {}

    # Only the ASSIGNABLE names are offered as reassignment targets (journal-only excluded).
    serialized_taxonomy = [
        {
            "name": c.get("name"),
            "definition": c.get("definition"),
            "umbrella_for": c.get("umbrella_for"),
            "kind": c.get("kind"),
        }
        for c in taxonomy
        if c.get("name") in ASSIGNABLE_NAME_SET
    ]

    assignments_by_category: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        assignments_by_category[d.category_name].append({
            "vendor_key": d.vendor_key,
            "confidence": d.confidence,
            "reasoning": d.reasoning,
        })

    prompt = SELF_CONSISTENCY.format(
        taxonomy_json=json.dumps(serialized_taxonomy, ensure_ascii=False, indent=2),
        assignments_by_category_json=json.dumps(
            assignments_by_category, ensure_ascii=False, indent=2,
        ),
    )

    if budget is not None:
        budget["used"] = budget.get("used", 0) + 1

    try:
        response = llm_call(prompt)
    except Exception as exc:
        logger.warning("Self-consistency LLM call failed: %s", exc)
        return taxonomy, decisions, {}

    if not isinstance(response, dict):
        return taxonomy, decisions, {}

    reassignments = response.get("reassignments") if isinstance(response.get("reassignments"), list) else []

    # The taxonomy is a FIXED, closed list — merges and renames are FORBIDDEN and
    # ignored entirely (a fixed government-sourced taxonomy is never mutated). The
    # only honoured output is a vendor-level reassignment to a different EXISTING
    # (assignable) category; anything off-list coerces to None (-> stored NULL).
    updated_taxonomy = [dict(c) for c in taxonomy]

    reassign_lookup = {}
    for r in reassignments:
        if not isinstance(r, dict):
            continue
        vk = r.get("vendor_key")
        target = (r.get("to") or "").strip()
        if vk and target:
            reassign_lookup[vk] = target

    updated_decisions: list[VendorDecision] = []
    for d in decisions:
        new_cat = d.category_name
        if d.vendor_key in reassign_lookup and reassign_lookup[d.vendor_key] in ASSIGNABLE_NAME_SET:
            new_cat = reassign_lookup[d.vendor_key]
        new_cat = coerce_to_valid(new_cat)
        updated_decisions.append(
            VendorDecision(
                vendor_key=d.vendor_key,
                category_name=new_cat,
                confidence=d.confidence,
                reasoning=d.reasoning,
                needs_refinement=d.needs_refinement,
            )
        )

    diff = {
        "merges": [],
        "renames": [],
        "reassignments": reassignments,
    }
    return updated_taxonomy, updated_decisions, diff
