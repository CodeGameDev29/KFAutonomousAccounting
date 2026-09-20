"""Invariants of the fixed Canada ISED/GIFI categorization taxonomy.

These lock the closed-set invariant: the classifier may only ASSIGN a name from
the fixed list — it can never invent, rename, or merge one, and any off-list /
journal-only output degrades to ``None`` (stored NULL / displayed
"Uncategorized"), never a guessed bucket.

The two semantics the rest of the engine depends on:
  * ``coerce_to_valid`` returns ``None`` for off-list AND journal-only names.
  * ``kind`` is the rich accounting value, not a coarse income/expense flag.
"""
from __future__ import annotations

import random

from config import ised_categories as ic

# The exact stored vocabulary, spelled out independently of the module under
# test. A typo here is a real bug.
LOCKED_STORED_NAMES = frozenset({
    # Revenue
    "Sales of goods and services", "All other revenues",
    # Cost of sales
    "Wages and benefits", "Purchases, materials and sub-contracts",
    "Opening inventory", "Closing inventory",
    # Operating
    "Labour and commissions", "Amortization and depletion",
    "Repairs and maintenance", "Utilities and telephone/telecommunication",
    "Rent", "Interest and bank charges", "Professional and business fees",
    "Advertising and promotion", "Delivery, shipping and warehouse expenses",
    "Insurance", "Travel", "Meals and entertainment", "Office supplies",
    "Software and IT", "Motor vehicle expenses", "Other indirect expenses",
    # Structural
    "Credit card payment", "Internal transfer", "Owner's draw",
    "Income tax and GST/HST remittance", "Refund/Credit",
})
JOURNAL_ONLY = frozenset({"Opening inventory", "Closing inventory", "Amortization and depletion"})


# ── The list itself ──────────────────────────────────────────────────────────
def test_fixed_taxonomy_is_exactly_the_locked_set():
    assert ic.CATEGORY_NAME_SET == LOCKED_STORED_NAMES
    assert len(ic.ISED_CATEGORIES) == 27
    names = [c.name for c in ic.ISED_CATEGORIES]
    assert len(names) == len(set(names)), "duplicate canonical names"


# ── Every row carries a kind, a bucket and a GIFI code ───────────────────────
def test_every_row_has_kind_gifi_bucket():
    valid_kinds = {"income", "cogs", "cogs_contra", "expense", "transfer", "equity", "tax", "contra"}
    reasonableness_slugs = {
        "operating_revenue", "cost_of_sales", "subcontracts", "salaries_wages",
        "employee_benefits", "professional_fees", "advertising", "rent", "insurance",
        "interest_bank", "cca_depreciation", "repairs_maintenance", "utilities",
        "delivery", "other_operating", "it_software", "office_supplies", "travel",
        "meals_entertainment", "motor_vehicle", "non_operating", "transfer", "distribution",
        # operating costs with no ISED peer line:
        "income_tax", "owner_draw",
    }
    # Owner's draw + income tax are counted as operating cost but have no GIFI
    # income-statement line.
    no_gifi_operating = {"owner_draw", "income_tax"}
    for c in ic.ISED_CATEGORIES:
        assert c.kind in valid_kinds, c.name
        assert c.ised_bucket is None or c.ised_bucket in reasonableness_slugs, c.name
        # GIFI present for every GIFI-mapped P&L line; None for structural rows
        # and for the two operating costs with no peer line.
        if c.section == "structural" or c.ised_bucket in no_gifi_operating:
            assert c.gifi is None, c.name
        else:
            assert c.gifi, c.name


# ── Journal-only rows ────────────────────────────────────────────────────────
def test_journal_only_rows_flagged():
    assert ic.JOURNAL_ONLY_NAMES == JOURNAL_ONLY
    for name in JOURNAL_ONLY:
        assert ic.is_journal_only(name)
        assert name not in ic.ASSIGNABLE_NAME_SET
    assert len(ic.ASSIGNABLE_NAME_SET) == 24


# ── coerce_to_valid is total ─────────────────────────────────────────────────
def test_coerce_to_valid_is_total():
    assert ic.coerce_to_valid("Travel") == "Travel"
    assert ic.coerce_to_valid("Crypto Mining Rig") is None
    assert ic.coerce_to_valid(None) is None
    assert ic.coerce_to_valid("") is None
    assert ic.coerce_to_valid("software and it") is None  # wrong case — exact match only
    # journal-only names are NOT assignable -> None
    assert ic.coerce_to_valid("Opening inventory") is None
    assert ic.coerce_to_valid("Amortization and depletion") is None


# ── Normalising a stored value ───────────────────────────────────────────────
def test_remap_legacy_keeps_canonical_and_drops_everything_else():
    """A name off the list can never reach a report as if it were a category."""
    for name in ic.CATEGORY_NAME_SET:
        assert ic.remap_legacy(name) == name
    # Journal-only names are canonical, which is what separates remap_legacy
    # from coerce_to_valid.
    assert ic.remap_legacy("Opening inventory") == "Opening inventory"
    assert ic.coerce_to_valid("Opening inventory") is None
    # Anything else, including a near-miss of a real name, is Uncategorized.
    for name in ("software and it", "Sales", "Crypto Mining Rig", "", "Uncategorized"):
        assert ic.remap_legacy(name) is None, name
    assert ic.remap_legacy(None) is None


# ── The rule pre-pass only emits canonical names ─────────────────────────────
def test_rule_prepass_only_emits_canonical():
    from core.categorization_agent.rule_prepass import CATEGORY_PATTERNS, match_category

    for category, _patterns, _nr in CATEGORY_PATTERNS:
        assert category in ic.CATEGORY_NAME_SET

    rng = random.Random(1234)
    vocab = ["MONTHLY FEE", "ESSO", "UPWORK", "AMAZON", "random junk", "", "   ",
             "INTEREST EARNED", "OUTGOING WIRE", "ORCHID TELECOM", "STAPLES",
             "CRA payment", "USD TFR", "REFUND", "ANTHROPIC", "zzz"]
    for _ in range(500):
        desc = " ".join(rng.choice(vocab) for _ in range(rng.randint(0, 4)))
        out = match_category(desc)
        assert out is None or out in ic.CATEGORY_NAME_SET, desc


# ── An unmatched description is Uncategorized ────────────────────────────────
def test_auto_categorize_unmatched_is_uncategorized():
    from core.categorization_agent.rule_prepass import _auto_categorize_transactions

    class T:
        def __init__(self, i, d):
            self.id, self.description = i, d

    out = _auto_categorize_transactions([T(1, "Some Random Vendor Inc")])
    assert out[1] == "Uncategorized"


# ── Labour tiebreak ──────────────────────────────────────────────────────────
def test_labour_tiebreak_defaults_to_operating():
    from core.categorization_agent.rule_prepass import match_category

    assert match_category("Cheque NO. 123") == "Labour and commissions"
    assert match_category("[CK]NO. 55") == "Labour and commissions"


# ── The core closed-set property ─────────────────────────────────────────────
def test_persisted_category_is_always_on_list():
    from datetime import date

    from core.categorization_agent.context import (
        BusinessProfile,
        CategorizationContext,
        TxnRow,
        VendorCluster,
        VendorDecision,
    )
    from core.categorization_agent.persistence import build_transaction_updates

    rng = random.Random(1234)
    hallucinated = ["Crypto Rig", "Vibes", "", "Consulting Revenue", "Opening inventory"]
    valid = list(ic.ASSIGNABLE_NAME_SET)

    txns, clusters, decisions, overrides = [], [], [], {}
    for i in range(1, 201):
        desc = f"vendor {i}"
        txns.append(TxnRow(
            id=i, date_posted=date(2026, 1, 1), amount=10.0, currency="CAD",
            account="BANK", description=desc, category=None,
            category_confirmed_by_user=False, vendor_key=f"v{i}",
        ))
        # 30% inject an off-list / journal-only name.
        cat = rng.choice(hallucinated) if rng.random() < 0.30 else rng.choice(valid)
        decisions.append(VendorDecision(f"v{i}", cat, "medium", "r", False))
        clusters.append(VendorCluster(
            vendor_key=f"v{i}", count=1, total_by_currency={"CAD": 10.0},
            date_range=(date(2026, 1, 1), date(2026, 1, 1)),
            sample_descriptions=[desc], sample_txn_ids=[i], transaction_ids=[i],
        ))
        if rng.random() < 0.30:
            overrides[i] = {"category": rng.choice(hallucinated), "confidence": "high", "reasoning": "x"}

    ctx = CategorizationContext(
        user_id="u", scope="all", business_profile=BusinessProfile("X", None, None, None),
        prior_taxonomy=[], prior_vendor_cache={}, confirmed_examples=[], transactions=txns,
    )
    updates = build_transaction_updates(
        context=ctx, clusters=clusters, decisions=decisions,
        refinement_overrides=overrides, rule_locked_ids=set(),
        txn_lookup={t.id: t for t in txns},
    )
    assert updates, "expected updates"
    for u in updates:
        # off-list / journal-only coerced to None (stored NULL); nothing else escapes.
        assert u["category"] is None or u["category"] in ic.CATEGORY_NAME_SET, u


# ── No LLM taxonomy synthesis ────────────────────────────────────────────────
def test_no_llm_taxonomy_synthesis():
    import inspect

    from core.categorization_agent import prompts, taxonomy

    # Phase 3 never calls an LLM and returns the fixed set.
    calls = []
    fixed, diff = taxonomy.synthesize_taxonomy(None, None, lambda p: calls.append(p))
    assert calls == []
    assert diff == {}
    assert {c["name"] for c in fixed} == ic.CATEGORY_NAME_SET

    # There is no name-inventor prompt, and taxonomy.py makes no LLM call.
    assert not hasattr(prompts, "TAXONOMY_SYNTHESIS")
    src = inspect.getsource(taxonomy)
    assert "TAXONOMY_SYNTHESIS" not in src
    assert "llm_call(" not in src


# ── Structural rows ──────────────────────────────────────────────────────────
def test_structural_rows_present_and_kinded():
    # Owner's draw and income tax count as OPERATING cost:
    # owner's draw kind=expense, income tax kind=tax.
    expected = {
        "Credit card payment": "transfer",
        "Internal transfer": "transfer",
        "Owner's draw": "expense",
        "Income tax and GST/HST remittance": "tax",
        "Refund/Credit": "contra",
    }
    for name, kind in expected.items():
        assert name in ic.CATEGORY_NAME_SET
        assert ic.kind_for(name) == kind
    # Only the two pure transfers are excluded from the P&L.
    assert ic.EXCLUDED_FROM_PL_NAMES == {"Credit card payment", "Internal transfer"}
    # Owner's draw is not an equity distribution — it counts as operating cost.
    assert ic.is_distribution("Owner's draw") is False
    assert ic.is_distribution("Income tax and GST/HST remittance") is False
    # Both group under Operating and count as an operating-cost bucket.
    assert ic.section_for("Owner's draw") == "operating"
    assert ic.section_for("Income tax and GST/HST remittance") == "operating"
    assert ic.ised_bucket_for("Owner's draw") == "owner_draw"
    assert ic.ised_bucket_for("Income tax and GST/HST remittance") == "income_tax"


# ── Wages and benefits (COGS) is reachable by the classifier ─────────────────
def test_wages_and_benefits_is_reachable():
    # It is a valid ASSIGNABLE name the LLM may pick on an explicit production
    # signal — the COGS line is not dead code.
    assert "Wages and benefits" in ic.ASSIGNABLE_NAME_SET
    assert ic.is_assignable("Wages and benefits")
    assert ic.section_for("Wages and benefits") == "cost_of_sales"
