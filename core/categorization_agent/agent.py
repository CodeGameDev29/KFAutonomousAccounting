"""Entry point: run all seven phases of the categorization agent."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime
from typing import Literal

from .consistency import run_consistency_check
from .context import (
    BusinessProfile,
    CategorizationContext,
    CategorizationRunResult,
    ConfirmedExample,
    TxnRow,
    UserCategory,
    VendorCacheEntry,
)
from .persistence import persist_run
from .progress import NullProgressReporter, ProgressReporter
from .refinement import refine_transactions
from .rule_prepass import match_category
from .taxonomy import synthesize_taxonomy
from .vendor_clustering import cluster_transactions
from .vendor_decisions import decide_vendor_categories

logger = logging.getLogger(__name__)


# Hard cap on LLM calls per run so a pathological input cannot issue an
# unbounded number of requests.
# Override via env var ``CATEGORIZATION_LLM_CALL_BUDGET``.
DEFAULT_LLM_BUDGET = int(os.environ.get("CATEGORIZATION_LLM_CALL_BUDGET", "200"))

# Token budget for this agent's replies, which are BATCH replies and so much
# longer than the engine's other JSON calls.
#
# The engine-wide default is 1024 (core.match_validator.ENGINE_JSON_MAX_TOKENS),
# sized for one match verdict with a sentence of reasoning. A Phase 4 group
# answers for up to VENDOR_GROUP_SIZE (20) vendors, each with a category, a
# confidence and a short reason, so that default truncates the object and
# core.llm_sync then spends a second round trip retrying it. 4096 clears a full
# 20-vendor group with room to spare: a reply that fits is a reply that parses.
CATEGORIZATION_JSON_MAX_TOKENS = int(
    os.environ.get("CATEGORIZATION_JSON_MAX_TOKENS", "4096")
)


def _wrap_llm_call(base_call, budget: dict):
    """Return an LLM helper that increments the shared budget counter.

    ``base_call`` is ``core.match_validator._call_llm`` or a stub in tests.
    Every call is tallied so Phase 4/5/6 can short-circuit when the budget is
    exhausted, instead of issuing calls without bound.

    Phases 4 and 5 call this from several threads at once, so the
    read-modify-write of the counter is taken under a lock — without it two
    concurrent calls can read the same value and one increment is lost, which
    is how a budget stops being a budget.
    """
    lock = threading.Lock()

    def wrapped(prompt: str):
        with lock:
            if budget["used"] >= budget["max"]:
                raise RuntimeError(
                    f"LLM call budget exhausted ({budget['max']}). "
                    "Increase CATEGORIZATION_LLM_CALL_BUDGET if needed."
                )
            budget["used"] += 1
        return base_call(prompt)
    return wrapped


def _load_context(db, user_id: str, scope: Literal["all", "uncategorized"]) -> CategorizationContext:
    """Phase 0 — fetch business profile, taxonomy, cache, examples, txns."""
    profile_row = db.get_business_profile() or {}
    business_profile = BusinessProfile(
        company_name=profile_row.get("company_name"),
        industry=profile_row.get("industry"),
        business_summary=profile_row.get("business_summary"),
        province=profile_row.get("province"),
    )

    taxonomy_rows = db.get_user_categories()
    prior_taxonomy = [
        UserCategory(
            id=row.get("id"),
            name=row.get("name"),
            definition=row.get("definition"),
            umbrella_for=row.get("umbrella_for"),
            example_vendors=row.get("example_vendors") or [],
            kind=row.get("kind"),
            is_system=bool(row.get("is_system", False)),
        )
        for row in taxonomy_rows
    ]

    prior_vendor_cache: dict[str, VendorCacheEntry] = {
        key: VendorCacheEntry(
            vendor_key=entry.get("vendor_key"),
            category_name=entry.get("category_name"),
            confidence=entry.get("confidence"),
            reasoning=entry.get("reasoning"),
            sample_desc=entry.get("sample_desc"),
        )
        for key, entry in db.get_vendor_cache().items()
    }

    confirmed_rows = db.get_confirmed_categorizations()
    confirmed_examples = [
        ConfirmedExample(
            txn_id=row.get("txn_id"),
            description=row.get("description") or "",
            amount=row.get("amount") or 0.0,
            currency=row.get("currency") or "CAD",
            category=row.get("category") or "",
        )
        for row in confirmed_rows
    ]

    txn_rows = db.get_transactions_for_categorization(scope)

    # Bulk-load matched receipts + linked-chain proof so every phase can
    # treat the matched vendor/line-items as the primary categorization
    # signal (description is secondary for matched rows).
    try:
        proof_by_txn = db.load_categorization_proof_context()
    except Exception as exc:
        logger.warning("load_categorization_proof_context failed: %s", exc)
        proof_by_txn = {}

    transactions: list[TxnRow] = []
    for row in txn_rows:
        dp = row.get("date_posted")
        if isinstance(dp, str):
            try:
                dp = date.fromisoformat(dp)
            except ValueError:
                continue
        if not isinstance(dp, date):
            continue
        txn_id = row.get("id")
        transactions.append(
            TxnRow(
                id=txn_id,
                date_posted=dp,
                amount=float(row.get("amount") or 0.0),
                currency=row.get("currency") or "CAD",
                account=row.get("account") or "",
                description=row.get("description") or "",
                category=row.get("category"),
                category_confirmed_by_user=bool(row.get("category_confirmed_by_user")),
                vendor_key=row.get("vendor_key"),
                note=row.get("note"),
                proof=proof_by_txn.get(txn_id),
            )
        )

    return CategorizationContext(
        user_id=user_id,
        scope=scope,
        business_profile=business_profile,
        prior_taxonomy=prior_taxonomy,
        prior_vendor_cache=prior_vendor_cache,
        confirmed_examples=confirmed_examples,
        transactions=transactions,
    )


def run_categorization_agent(
    db,
    user_id: str,
    scope: Literal["all", "uncategorized"],
    progress: ProgressReporter | None = None,
) -> CategorizationRunResult:
    """Execute all seven phases and return a structured result.

    This is the single public entry point used by both reconciliation
    and the Recategorize button.
    """
    from core.match_validator import _call_llm

    progress = progress or NullProgressReporter()
    started_at = datetime.utcnow()
    t0 = time.time()
    budget = {"used": 0, "max": DEFAULT_LLM_BUDGET}
    # Label the calls as this agent's own so a JSON failure is logged and
    # tallied under "categorization", not under the plumbing.
    llm_call = _wrap_llm_call(
        lambda prompt: _call_llm(
            prompt,
            label="categorization",
            max_tokens=CATEGORIZATION_JSON_MAX_TOKENS,
        ),
        budget,
    )

    try:
        # ── Phase 0 — context load ──
        progress.log("Loading context", 2, "Loading context for categorization agent...")
        context = _load_context(db, user_id, scope)
        profile_short = (
            f"{context.business_profile.company_name or 'Unknown'}"
            f" | industry: {context.business_profile.industry or 'unknown'}"
            f" | province: {context.business_profile.province or 'unknown'}"
        )
        progress.log(
            "Loading context", 5,
            f"[Categorization] Loaded {len(context.transactions)} transactions | "
            f"business: \"{profile_short}\" | "
            f"{len(context.confirmed_examples)} confirmed categorizations | "
            f"{len(context.prior_taxonomy)} existing categories",
        )
        progress.set_total(len(context.transactions))

        if not context.transactions:
            progress.log("Complete", 100, "[Categorization] No transactions in scope — nothing to do.")
            completed_at = datetime.utcnow()
            _persist_empty_run(db, context, started_at, completed_at, budget["used"])
            return CategorizationRunResult(
                status="complete",
                total=0,
                rule_locked=0,
                vendors_categorized=0,
                txns_refined=0,
                categories_final=len(context.prior_taxonomy),
                llm_calls=budget["used"],
                taxonomy_diff={},
                started_at=started_at,
                completed_at=completed_at,
            )

        txn_lookup: dict[int, TxnRow] = {t.id: t for t in context.transactions}

        # ── Phase 1 — rule pre-pass ──
        progress.log("Rule pre-pass", 8, "[Categorization] Applying deterministic rule patterns...")
        rule_locked_ids: set[int] = set()
        non_locked: list[TxnRow] = []
        for t in context.transactions:
            if t.category_confirmed_by_user:
                continue  # Hard-locked; skip every phase.
            if match_category(t.description):
                rule_locked_ids.add(t.id)
            else:
                non_locked.append(t)
        progress.log(
            "Rule pre-pass", 12,
            f"[Categorization] Rule pre-pass locked {len(rule_locked_ids)}/{len(context.transactions)} "
            f"transactions (bank fees, transfers, CRA).",
        )

        # ── Phase 2 — vendor clustering ──
        progress.log("Clustering vendors", 15, "[Categorization] Normalizing vendor descriptions...")
        clusters, key_by_id = cluster_transactions(non_locked, llm_call=llm_call)
        top_vendors = ", ".join(
            f"{c.vendor_key} x{c.count}" for c in clusters[:3]
        ) or "(none)"
        progress.log(
            "Clustering vendors", 20,
            f"[Categorization] Clustered {len(non_locked)} remaining transactions into "
            f"{len(clusters)} unique vendors (top: {top_vendors}).",
        )

        if not clusters:
            progress.log("Persisting", 95, "[Categorization] No vendors to categorize beyond rules.")
            completed_at = datetime.utcnow()
            # Persist just the rule-locked rows.
            run_id, rows_updated = persist_run(
                db=db,
                context=context,
                final_taxonomy=[
                    {
                        "name": c.name,
                        "definition": c.definition,
                        "umbrella_for": c.umbrella_for,
                        "example_vendors": c.example_vendors,
                        "kind": c.kind,
                        "is_system": c.is_system,
                    }
                    for c in context.prior_taxonomy
                ],
                decisions=[],
                clusters=[],
                refinement_overrides={},
                rule_locked_ids=rule_locked_ids,
                txn_lookup=txn_lookup,
                started_at=started_at,
                completed_at=completed_at,
                llm_calls=budget["used"],
                taxonomy_diff={},
                consistency_diff={},
                status="complete",
            )
            progress.log("Complete", 100, f"[Categorization] Persisted {rows_updated} rule-locked rows. Done.")
            return CategorizationRunResult(
                status="complete",
                total=len(context.transactions),
                rule_locked=len(rule_locked_ids),
                vendors_categorized=0,
                txns_refined=0,
                categories_final=len(context.prior_taxonomy),
                llm_calls=budget["used"],
                taxonomy_diff={},
                started_at=started_at,
                completed_at=completed_at,
            )

        # ── Phase 3 — taxonomy synthesis ──
        progress.log("Synthesizing taxonomy", 25, "[Categorization] Synthesizing business-aware taxonomy...")
        final_taxonomy, taxonomy_diff = synthesize_taxonomy(context, clusters, llm_call)
        added = taxonomy_diff.get("added") or []
        renamed = taxonomy_diff.get("renamed") or []
        progress.log(
            "Synthesizing taxonomy", 35,
            f"[Categorization] Synthesized taxonomy: {len(final_taxonomy)} categories "
            f"({max(len(final_taxonomy) - len(added), 0)} reused, {len(added)} added, {len(renamed)} renamed).",
        )
        for name in added[:5]:
            progress.log("Synthesizing taxonomy", 36, f"  + Added: \"{name}\"")
        for item in renamed[:5]:
            if isinstance(item, dict):
                progress.log(
                    "Synthesizing taxonomy", 36,
                    f"  ~ Renamed: \"{item.get('from')}\" → \"{item.get('to')}\"",
                )

        # ── Phase 4 — vendor-level categorization ──
        progress.log("Categorizing vendors", 40, "[Categorization] Deciding category per vendor cluster...")
        seed_decisions = {
            key: entry.category_name
            for key, entry in context.prior_vendor_cache.items()
            if entry.category_name
        }

        def _on_decision(decision, done, total):
            pct = 40 + int(35 * done / max(total, 1))
            tag = "deferred" if decision.needs_refinement else decision.confidence
            progress.log(
                "Categorizing vendors", pct,
                f"[Categorization] Vendor {done}/{total}: {decision.vendor_key} → "
                f"{decision.category_name} ({tag})",
            )
            progress.set_done(done)

        decisions = decide_vendor_categories(
            clusters=clusters,
            taxonomy=final_taxonomy,
            business_profile_text=context.business_profile.as_prompt_lines(),
            seed_decisions=seed_decisions,
            llm_call=llm_call,
            on_decision=_on_decision,
            budget=budget,
        )

        # ── Phase 5 — per-transaction refinement ──
        mixed = [
            cluster for cluster in clusters
            if any(
                d.needs_refinement
                for d in decisions
                if d.vendor_key == cluster.vendor_key
            )
        ]
        if mixed:
            mixed_vendors = ", ".join(c.vendor_key for c in mixed[:5])
            refine_count = sum(len(c.transaction_ids) for c in mixed)
            progress.log(
                "Refining", 78,
                f"[Categorization] Refining {refine_count} transactions across "
                f"{len(mixed)} mixed-use vendors ({mixed_vendors})...",
            )
            refinement_overrides = refine_transactions(
                mixed_clusters=mixed,
                txn_lookup=txn_lookup,
                taxonomy=final_taxonomy,
                business_profile_text=context.business_profile.as_prompt_lines(),
                llm_call=llm_call,
                budget=budget,
            )
            progress.log(
                "Refining", 85,
                f"[Categorization] Refinement produced {len(refinement_overrides)} per-txn overrides.",
            )
        else:
            refinement_overrides = {}
            progress.log(
                "Refining", 85,
                "[Categorization] No mixed-use vendors flagged for refinement.",
            )

        # ── Phase 6 — self-consistency check ──
        progress.log("Consistency check", 88, "[Categorization] Running self-consistency pass...")
        final_taxonomy, decisions, consistency_diff = run_consistency_check(
            taxonomy=final_taxonomy,
            decisions=decisions,
            llm_call=llm_call,
            budget=budget,
        )
        if not any([
            consistency_diff.get("merges"),
            consistency_diff.get("reassignments"),
            consistency_diff.get("renames"),
        ]):
            progress.log("Consistency check", 90, "[Categorization] Self-consistency: clean.")
        else:
            for m in (consistency_diff.get("merges") or [])[:5]:
                progress.log(
                    "Consistency check", 90,
                    f"[Categorization] Self-consistency: merged \"{m.get('from')}\" "
                    f"into \"{m.get('into')}\" ({m.get('reason', '')}).",
                )

        # ── Phase 7 — persist ──
        progress.log("Persisting", 93, "[Categorization] Writing taxonomy, vendor cache, and transaction updates...")
        completed_at = datetime.utcnow()
        run_id, rows_updated = persist_run(
            db=db,
            context=context,
            final_taxonomy=final_taxonomy,
            decisions=decisions,
            clusters=clusters,
            refinement_overrides=refinement_overrides,
            rule_locked_ids=rule_locked_ids,
            txn_lookup=txn_lookup,
            started_at=started_at,
            completed_at=completed_at,
            llm_calls=budget["used"],
            taxonomy_diff=taxonomy_diff,
            consistency_diff=consistency_diff,
            status="complete",
        )

        elapsed = time.time() - t0
        mins, secs = divmod(int(elapsed), 60)
        progress.log(
            "Complete", 100,
            f"[Categorization] Persisted {len(decisions)} vendor-level decisions, "
            f"{len(refinement_overrides)} refined txns, {len(final_taxonomy)} categories. "
            f"Run complete in {mins}m {secs}s.",
        )

        return CategorizationRunResult(
            status="complete",
            total=len(context.transactions),
            rule_locked=len(rule_locked_ids),
            vendors_categorized=len(decisions),
            txns_refined=len(refinement_overrides),
            categories_final=len(final_taxonomy),
            llm_calls=budget["used"],
            taxonomy_diff=taxonomy_diff,
            started_at=started_at,
            completed_at=completed_at,
        )
    except Exception as exc:
        logger.exception("Categorization agent failed")
        progress.log("Error", 100, f"[Categorization] Failed: {exc}")
        try:
            _persist_error_run(db, user_id, scope, started_at, budget["used"], str(exc))
        except Exception:
            logger.exception("Failed to write categorization_run audit row for error case")
        return CategorizationRunResult(
            status="error",
            total=0,
            rule_locked=0,
            vendors_categorized=0,
            txns_refined=0,
            categories_final=0,
            llm_calls=budget["used"],
            taxonomy_diff={},
            started_at=started_at,
            completed_at=datetime.utcnow(),
            error=str(exc),
        )


def _persist_empty_run(db, context, started_at, completed_at, llm_calls):
    """Write an audit row for an empty run (no transactions in scope)."""
    db.insert_categorization_run({
        "started_at": started_at,
        "completed_at": completed_at,
        "scope": context.scope,
        "total": 0,
        "categorized": 0,
        "rule_locked": 0,
        "llm_calls": llm_calls,
        "status": "complete",
        "error": None,
        "summary": {"phases": {"skipped": "nothing_in_scope"}},
    })


def _persist_error_run(db, user_id, scope, started_at, llm_calls, error):
    """Write an audit row for a failed run."""
    db.insert_categorization_run({
        "started_at": started_at,
        "completed_at": datetime.utcnow(),
        "scope": scope,
        "total": 0,
        "categorized": 0,
        "rule_locked": 0,
        "llm_calls": llm_calls,
        "status": "error",
        "error": error,
        "summary": None,
    })
