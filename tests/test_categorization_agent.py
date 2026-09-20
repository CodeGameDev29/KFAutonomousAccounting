"""Unit tests for the categorization agent module.

Every transaction, note, vendor and amount below is invented — the company is
``Example Corp Inc.`` and no book outside this file is represented. These tests
exercise pure helpers (rule pre-pass, vendor clustering, persistence update
assembly, consistency rewrite, batch-reply reading, and the end-to-end
orchestrator against stub objects). They never reach a database or an LLM —
every LLM call is replaced with a deterministic stub.

Where a test asserts a *category*, it asserts against the rule table the repo
ships (``config/category_rules.yaml``) rather than against a copy of it, so the
expectations move with the shipped rules: bank fees, interest earned, incoming
wires, payroll direct deposits, refunds and the four money-transfer fee
patterns. No rule that names a specific retailer is exercised, so this file
introduces no merchant of its own.
"""

from __future__ import annotations

from datetime import date

from core.categorization_agent.consistency import run_consistency_check
from core.categorization_agent.context import (
    BusinessProfile,
    CategorizationContext,
    TxnRow,
    VendorCluster,
    VendorDecision,
)
from core.categorization_agent.persistence import build_transaction_updates
from core.categorization_agent.rule_prepass import (
    _auto_categorize_transactions,
    match_category,
)
from core.categorization_agent.vendor_clustering import (
    cluster_transactions,
    normalize_description,
)

# ── Phase 1: rule pre-pass ───────────────────────────────────────────────


def test_rule_prepass_locks_bank_fee():
    """The three shapes a rule can settle with no context at all: a fee the bank
    charged, interest the bank paid, and money arriving by wire."""
    assert match_category("MONTHLY FEE CASH MGMT") == "Interest and bank charges"
    assert match_category("INTEREST EARNED 2026-01-31") == "All other revenues"
    assert match_category("INCOMING WIRE from client") == "Sales of goods and services"


def test_rule_prepass_leaves_unknown_vendors_alone():
    assert match_category("Harbourline Logistics Ltd.") is None


def test_user_notes_flow_into_categorization_signals():
    """User notes are collected into clusters and serialized for the LLM as an
    authoritative categorization signal: the user knows their own transactions,
    so a note must never be dropped on the way to the model."""
    from core.categorization_agent.refinement import _serialize_txn_for_refinement
    from core.categorization_agent.vendor_clustering import cluster_transactions
    from core.categorization_agent.vendor_decisions import _serialize_cluster_for_decision

    t_no_note = _txn(1, "OnlineTransfer, TF 000123")
    t_noted = _txn(2, "OnlineTransfer, TF 000123")
    t_noted.note = "Credit Card payoff next month"

    clusters, _ = cluster_transactions([t_no_note, t_noted])
    c = next(cl for cl in clusters if 2 in cl.transaction_ids)
    # the note is carried on the cluster and offered to the vendor-decision LLM
    assert "Credit Card payoff next month" in c.sample_notes
    assert "Credit Card payoff next month" in _serialize_cluster_for_decision(c)["user_notes"]
    # and to the per-txn refinement LLM
    assert _serialize_txn_for_refinement(t_noted)["user_note"] == "Credit Card payoff next month"
    # a txn with no note carries no user_note key
    assert "user_note" not in _serialize_txn_for_refinement(t_no_note)


def test_auto_categorize_transactions_preserves_ids():
    """Two different rules and the no-rule fallback, keyed back to the right id."""

    class T:
        def __init__(self, id_: int, desc: str) -> None:
            self.id = id_
            self.description = desc

    result = _auto_categorize_transactions([
        T(1, "MONTHLY FEE"),
        T(2, "REFUND FOR ORDER 4410"),
        T(3, "Harbourline Logistics Ltd."),
    ])
    assert result[1] == "Interest and bank charges"
    assert result[2] == "Refund/Credit"
    assert result[3] == "Uncategorized"


# ── Phase 2: vendor clustering ───────────────────────────────────────────


def test_normalize_description_strips_reference_numbers():
    """A star code, a posting date and a legal-form suffix are noise; what is
    left is the merchant. The last two land on the canonical alias table, the
    first on the slug fallback — both have to be stable across runs."""
    assert normalize_description("CEDARVIEW SUPPLIES LTD*H1234567 CDRV.CA/BILL") == "cedarview_supplies"
    assert normalize_description("WISE PAYMENTS LTD LONDON") == "wise"
    assert normalize_description("STRIPE PAYMENT 20260101") == "stripe"


def _txn(id_: int, desc: str, amount: float = -10.0) -> TxnRow:
    return TxnRow(
        id=id_,
        date_posted=date(2026, 1, 1),
        amount=amount,
        currency="CAD",
        account="CAD",
        description=desc,
        category=None,
        category_confirmed_by_user=False,
    )


def test_cluster_transactions_groups_by_canonical_key():
    txns = [
        _txn(1, "CEDARVIEW SUPPLIES LTD*H1234567"),
        _txn(2, "CEDARVIEW SUPPLIES INC 2026-01-05"),
        _txn(3, "WISE PAYMENTS LTD"),
    ]
    clusters, key_by_id = cluster_transactions(txns, llm_call=lambda _p: {})
    keys = {c.vendor_key for c in clusters}
    assert "cedarview_supplies" in keys
    assert "wise" in keys
    assert key_by_id[1] == "cedarview_supplies"
    assert key_by_id[2] == "cedarview_supplies"
    assert key_by_id[3] == "wise"


# ── Phase 6: self-consistency ────────────────────────────────────────────


def test_consistency_never_merges_fixed_names():
    """The taxonomy is a fixed closed list — merges/renames must be IGNORED,
    and no decision may end up outside the fixed set."""
    from config.ised_categories import CATEGORY_NAME_SET, taxonomy_upsert_rows

    taxonomy = taxonomy_upsert_rows()
    decisions = [
        VendorDecision("bluepeak_software", "Software and IT", "high", "LLM", False),
        VendorDecision("cedarview_supplies", "Office supplies", "high", "LLM", False),
    ]

    def stub_llm(_prompt: str) -> dict:
        # The model tries to merge/rename a fixed category — must be rejected.
        return {
            "merges": [{"from": "Software and IT", "into": "Travel", "reason": "dup"}],
            "reassignments": [],
            "renames": [{"from": "Travel", "to": "Trips"}],
        }

    new_tax, new_decs, diff = run_consistency_check(
        taxonomy=taxonomy, decisions=decisions, llm_call=stub_llm,
    )
    names = {c["name"] for c in new_tax}
    # Fixed taxonomy is unchanged; both fixed names still present.
    assert "Software and IT" in names
    assert "Travel" in names
    assert "Trips" not in names
    # No decision left the fixed set.
    assert all(d.category_name in CATEGORY_NAME_SET for d in new_decs)
    # Merges/renames are always empty under the fixed taxonomy.
    assert diff["merges"] == []
    assert diff["renames"] == []


# ── Phase 7: persistence assembly ────────────────────────────────────────


def _context_for(txns: list[TxnRow]) -> CategorizationContext:
    return CategorizationContext(
        user_id="user-example",
        scope="all",
        business_profile=BusinessProfile("Example Corp Inc.", None, None, None),
        prior_taxonomy=[],
        prior_vendor_cache={},
        confirmed_examples=[],
        transactions=txns,
    )


def test_build_transaction_updates_rule_lock_wins():
    txns = [_txn(1, "MONTHLY FEE"), _txn(2, "BLUEPEAK SOFTWARE INC")]
    txn_lookup = {t.id: t for t in txns}
    ctx = _context_for(txns)
    clusters = [
        VendorCluster(
            vendor_key="bluepeak_software",
            count=1,
            total_by_currency={"CAD": 10.0},
            date_range=(date(2026, 1, 1), date(2026, 1, 1)),
            sample_descriptions=["BLUEPEAK SOFTWARE INC"],
            sample_txn_ids=[2],
            transaction_ids=[2],
        )
    ]
    decisions = [VendorDecision("bluepeak_software", "Software and IT", "high", "LLM", False)]

    updates = build_transaction_updates(
        context=ctx,
        clusters=clusters,
        decisions=decisions,
        refinement_overrides={},
        rule_locked_ids={1},
        txn_lookup=txn_lookup,
    )
    by_id = {u["id"]: u for u in updates}
    assert by_id[1]["category_source"] == "rule"
    assert by_id[1]["category"] == "Interest and bank charges"
    assert by_id[2]["category_source"] == "agent"
    assert by_id[2]["category"] == "Software and IT"


def test_build_transaction_updates_refinement_override_wins():
    """Phase 5 looked at this one transaction; Phase 4 only looked at the vendor.
    The narrower look wins, and its reasoning is what gets stored."""
    txns = [_txn(1, "CEDARVIEW SUPPLIES LTD*H1234567", amount=-120.00)]
    txn_lookup = {t.id: t for t in txns}
    ctx = _context_for(txns)
    clusters = [
        VendorCluster(
            vendor_key="cedarview_supplies",
            count=1,
            total_by_currency={"CAD": 120.0},
            date_range=(date(2026, 1, 1), date(2026, 1, 1)),
            sample_descriptions=["CEDARVIEW SUPPLIES LTD"],
            sample_txn_ids=[1],
            transaction_ids=[1],
        )
    ]
    decisions = [VendorDecision("cedarview_supplies", "Office supplies", "medium", "", True)]
    overrides = {1: {"category": "Advertising and promotion", "confidence": "high",
                     "reasoning": "printed flyers for the January campaign"}}

    updates = build_transaction_updates(
        context=ctx,
        clusters=clusters,
        decisions=decisions,
        refinement_overrides=overrides,
        rule_locked_ids=set(),
        txn_lookup=txn_lookup,
    )
    assert updates[0]["category"] == "Advertising and promotion"
    assert updates[0]["category_reasoning"] == "printed flyers for the January campaign"


def test_uncertainty_note_forces_uncategorized():
    """A user note saying they don't know what a txn is leaves it Uncategorized,
    overriding the LLM's guess. Never guess against the user's stated
    uncertainty — a wrong category on a CRA filing is worse than a blank one."""
    t = _txn(1, "MERIDIAN COURIER SERVICES", amount=-12.0)
    t.note = "Uncertain what this is"
    ctx = _context_for([t])
    clusters = [VendorCluster(
        vendor_key="meridian_courier", count=1, total_by_currency={"CAD": 12.0},
        date_range=(date(2026, 1, 1), date(2026, 1, 1)),
        sample_descriptions=["MERIDIAN COURIER SERVICES"],
        sample_txn_ids=[1], transaction_ids=[1],
    )]
    # LLM wrongly guessed revenue; the uncertainty note must override it.
    decisions = [VendorDecision("meridian_courier", "Sales of goods and services", "high", "guess", False)]
    updates = build_transaction_updates(
        context=ctx, clusters=clusters, decisions=decisions,
        refinement_overrides={}, rule_locked_ids=set(), txn_lookup={1: t},
    )
    assert updates[0]["category"] is None
    assert updates[0]["category_reasoning"]  # reasoning is recorded


def test_refund_note_overrides_rule_lock():
    """An explicit refund note flips a rule-locked row (a DirectDeposit the rule
    calls revenue) to Refund/Credit — money coming back is not a sale."""
    t = _txn(1, "DirectDeposit, PAYPAL", amount=104.55)
    t.note = "Retailer purchase refunded"
    ctx = _context_for([t])
    updates = build_transaction_updates(
        context=ctx, clusters=[], decisions=[], refinement_overrides={},
        rule_locked_ids={1}, txn_lookup={1: t},
    )
    assert updates[0]["category"] == "Refund/Credit"
    # An informational note (no refund/uncertainty signal) must NOT override.
    t2 = _txn(2, "MONTHLY FEE")
    t2.note = "$300 gift card used in addition; see the receipt"
    updates2 = build_transaction_updates(
        context=ctx, clusters=[], decisions=[], refinement_overrides={},
        rule_locked_ids={2}, txn_lookup={2: t2},
    )
    assert updates2[0]["category"] == "Interest and bank charges"  # rule kept


# ── End-to-end orchestration against stubs ───────────────────────────────


class _StubDb:
    def __init__(self, transactions: list[TxnRow]) -> None:
        self._txns = transactions
        self.taxonomy_upserts: list[list[dict]] = []
        self.vendor_cache_upserts: list[list[dict]] = []
        self.txn_updates: list[list[dict]] = []
        self.runs: list[dict] = []
        self._next_run_id = 1

    def get_business_profile(self) -> dict:
        return {"company_name": "Example Corp Inc.", "industry": "Software", "province": "MB"}

    def get_user_categories(self) -> list[dict]:
        return []

    def get_vendor_cache(self) -> dict:
        return {}

    def get_confirmed_categorizations(self) -> list[dict]:
        return []

    def load_categorization_proof_context(self) -> dict:
        return {}

    def get_transactions_for_categorization(self, scope: str) -> list[dict]:
        return [
            {
                "id": t.id,
                "date_posted": t.date_posted,
                "amount": t.amount,
                "currency": t.currency,
                "account": t.account,
                "description": t.description,
                "category": t.category,
                "category_confirmed_by_user": t.category_confirmed_by_user,
                "vendor_key": t.vendor_key,
            }
            for t in self._txns
        ]

    def upsert_user_categories(self, rows: list[dict]) -> None:
        self.taxonomy_upserts.append(rows)

    def upsert_vendor_cache(self, rows: list[dict]) -> None:
        self.vendor_cache_upserts.append(rows)

    def update_transaction_categorizations(self, updates: list[dict]) -> int:
        self.txn_updates.append(updates)
        return len(updates)

    def insert_categorization_run(self, run: dict) -> int:
        self.runs.append(run)
        rid = self._next_run_id
        self._next_run_id += 1
        return rid


def test_budget_cap_short_circuits(monkeypatch):
    """With budget=0, the agent returns a clean result but never calls the LLM."""
    from core.categorization_agent import agent as agent_mod

    fake_llm_calls: list[str] = []

    def fake_call_llm(prompt: str, **kwargs):
        fake_llm_calls.append(prompt)
        return []

    monkeypatch.setattr("core.match_validator._call_llm", fake_call_llm)
    monkeypatch.setattr(agent_mod, "DEFAULT_LLM_BUDGET", 0)

    txns = [
        _txn(1, "MONTHLY FEE"),
        _txn(2, "Harbourline Logistics Ltd."),
    ]
    db = _StubDb(txns)
    result = agent_mod.run_categorization_agent(db, "user-example", "all")

    assert result.status == "complete"
    assert result.rule_locked == 1  # the MONTHLY FEE row
    assert result.llm_calls <= 1  # at most the consistency check guard, no LLM actually dispatched
    assert fake_llm_calls == []  # wrapper short-circuits before calling


def test_agent_persists_rule_locked_rows(monkeypatch):
    """All-rules run: zero LLM calls, still writes updates + audit row."""
    from core.categorization_agent import agent as agent_mod

    monkeypatch.setattr("core.match_validator._call_llm", lambda _p, **kw: [])

    txns = [
        _txn(1, "MONTHLY FEE"),
        _txn(2, "INCOMING WIRE from client"),
    ]
    db = _StubDb(txns)
    result = agent_mod.run_categorization_agent(db, "user-example", "all")

    assert result.status == "complete"
    assert result.rule_locked == 2
    assert db.runs, "expected categorization_run audit row"
    update_batch = db.txn_updates[-1]
    assert {u["id"] for u in update_batch} == {1, 2}
    assert all(u["category_source"] == "rule" for u in update_batch)


def test_confirmed_rows_skipped_in_phase_1(monkeypatch):
    monkeypatch.setattr("core.match_validator._call_llm", lambda _p, **kw: [])

    confirmed = _txn(1, "MONTHLY FEE")
    confirmed.category_confirmed_by_user = True
    confirmed.category = "Office supplies"

    db = _StubDb([confirmed])
    from core.categorization_agent import agent as agent_mod
    result = agent_mod.run_categorization_agent(db, "user-example", "all")
    assert result.status == "complete"
    # No update batches should touch the confirmed row.
    all_updates = [u for batch in db.txn_updates for u in batch]
    assert all(u["id"] != 1 for u in all_updates)


# ── The bounded pool that carries Phases 4 and 5 ──────────────────────────
#
# Both phases ask independent questions — a group of 20 vendors, a batch of 10
# transactions — so their calls go out together rather than one at a time, and
# the pool is bounded by the same knob the matcher uses.


def test_run_bounded_keeps_the_input_order():
    from core.categorization_agent.concurrency import run_bounded

    items = list(range(20))
    assert run_bounded(items, lambda i: i * 2) == [i * 2 for i in items]


def test_run_bounded_actually_runs_them_together():
    import threading
    import time

    from core.categorization_agent import concurrency

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def slow(i: int) -> int:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return i

    assert concurrency.run_bounded(list(range(8)), slow) == list(range(8))
    assert peak > 1


def test_run_bounded_never_exceeds_the_knob(monkeypatch):
    import threading
    import time

    from core.categorization_agent import concurrency

    monkeypatch.setattr(concurrency, "MAX_CONCURRENT_CALLS", 3)
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def slow(i: int) -> int:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return i

    concurrency.run_bounded(list(range(12)), slow)
    assert peak <= 3


def test_run_bounded_on_one_item_does_not_start_a_pool():
    import threading

    from core.categorization_agent.concurrency import run_bounded

    seen: list[int] = []
    run_bounded(["only"], lambda _x: seen.append(threading.get_ident()))
    assert seen == [threading.get_ident()]


def test_the_pool_is_the_engine_knob():
    """Not the linker's 4: a categorization call is matcher-shaped work."""
    from config.settings import local_llm_config
    from core.categorization_agent.concurrency import MAX_CONCURRENT_CALLS

    assert MAX_CONCURRENT_CALLS == local_llm_config.engine_max_concurrent


# ── Transfer-platform fees: the rule, not the receipt matcher ────────────
#
# One invented page of a transfer-platform (Wise) USD activity export for
# Example Corp Inc. Each outgoing transfer is billed as TWO statement rows: the
# transfer itself, and the platform's own fee on the next line. The fee rows are
# bank charges and need no receipt; the transfer rows are sub-contract costs and
# do. Getting that backwards is how a fee row reaches the receipt matcher, draws
# a weak match against whatever receipt is nearest in amount, and comes out
# categorized as whatever that receipt was.

PLATFORM_FEE_ROWS = (
    "Wise Charges for: TRANSFER-00000000",   # -8.60 USD, 2026-03-03
    "Wise Charges for: TRANSFER-00000001",   # -14.25 USD, 2026-03-03
    "Wise Charges for: TRANSFER-00000002",   # -8.60 USD, 2026-01-07
    "Wise Charges for: TRANSFER-00000003",   # -14.25 USD, 2026-02-03
)

# The rows that sit immediately around them in the same export. None of these is
# a bank charge, and the fee rule must not touch any of them — note that three
# of them contain the word "fee", because the platform prints the fee it billed
# separately inside the transfer's own description.
PLATFORM_NEIGHBOUR_ROWS = (
    "Sent money to Jordan Avery (fee: 8.60 USD)",
    "Sent money to Riley Chen (fee: 14.25 USD)",
    "Sent money to Sam Okafor (fee: 14.25 USD)",
    "Received money from Harbourline Logistics Ltd. with reference Harbourline Contract Work",
    "Received money from Northwind Trading Co. with reference Northwind Development",
)


def test_wise_transfer_fee_rows_are_bank_charges():
    from core.categorization_agent.rule_prepass import is_non_reconcilable

    for desc in PLATFORM_FEE_ROWS:
        assert match_category(desc) == "Interest and bank charges", desc
        # non_reconcilable => the reconciliation API stamps IGNORED and keeps
        # the row out of the match loop entirely.
        assert is_non_reconcilable(desc) is True, desc


def test_wise_fee_rule_does_not_swallow_the_transfer_it_belongs_to():
    """'(fee: 8.60 USD)' is the contractor payment, not the 8.60 fee."""
    from core.categorization_agent.rule_prepass import is_non_reconcilable

    for desc in PLATFORM_NEIGHBOUR_ROWS:
        assert match_category(desc) != "Interest and bank charges", desc
        assert is_non_reconcilable(desc) is False, desc

    # No rule in the table claims them at all, so they reach the matcher.
    assert match_category("Sent money to Jordan Avery (fee: 8.60 USD)") is None
    assert match_category(
        "Received money from Northwind Trading Co. with reference Northwind Development"
    ) is None


def test_fee_rule_survives_a_wording_change_by_wise():
    """The platform owns the wording; the rule covers the fee shapes, not one string."""
    for desc in (
        "Wise fee: TRANSFER-00000000",
        "Wise transfer fee for TRANSFER-00000000",
        "Wise charged a fee",
        "Fee for: TRANSFER-00000000",
    ):
        assert match_category(desc) == "Interest and bank charges", desc


def test_a_bare_fee_mention_is_never_a_bank_charge():
    """Guard the narrowness: no pattern here may fire on the word alone."""
    for desc in (
        "PAYPAL TRANSFER (fee included)",
        "Invoice 4410 fee schedule",
        "Sent money to Priya Nair (fee: 8.60 USD)",
    ):
        assert match_category(desc) != "Interest and bank charges", desc


# ── Stale vendor keys must not outlive the proof that made them ──────────


def test_vendor_key_is_recomputed_from_the_description_each_run():
    """A vendor key written by an earlier run outlives the proof that produced
    it unless the key is recomputed, so a row whose description does not
    support the stored key does not inherit it."""
    stale = _txn(101, "Wise Charges for: TRANSFER-00000000")
    stale.vendor_key = "cedarview_supplies"  # written by a prior, proof-fed run

    _clusters, key_by_id = cluster_transactions([stale], llm_call=lambda _p: {})
    assert key_by_id[101] == "wise"


def test_approved_proof_still_wins_over_the_description():
    """The description is a processor wrapper; the matched receipt names the
    real merchant, so the proof vendor keys the cluster."""
    matched = _txn(500, "General Currency Conversion")
    matched.proof = {"vendor": "Bluepeak Software Inc", "source": "direct", "link_depth": 0}

    _clusters, key_by_id = cluster_transactions([matched], llm_call=lambda _p: {})
    assert key_by_id[500] == "bluepeak_software"
    # Without the proof this row would have clustered as the processor action.
    assert normalize_description("General Currency Conversion") == "paypal_fx"


def test_a_stored_key_is_still_reused_when_the_description_has_no_vendor():
    """Descriptions the regex cannot read keep their LLM-normalized key, so a
    re-run does not pay for the same normalization round trip twice."""
    opaque = _txn(600, "$$$")
    opaque.vendor_key = "orchid_telecom"

    _clusters, key_by_id = cluster_transactions([opaque], llm_call=None)
    assert key_by_id[600] == "orchid_telecom"


# ── Phase 4: reading a batch reply out of whatever shape came back ───────


def _clusters_for(*keys: str) -> list[VendorCluster]:
    return [
        VendorCluster(
            vendor_key=k,
            count=1,
            total_by_currency={"CAD": 10.0},
            date_range=(date(2026, 1, 1), date(2026, 1, 31)),
            sample_descriptions=[k.upper()],
            sample_txn_ids=[i],
            transaction_ids=[i],
        )
        for i, k in enumerate(keys, start=1)
    ]


def _decide(response, keys=("wise", "cedarview_supplies")):
    from core.categorization_agent.vendor_decisions import decide_vendor_categories

    return decide_vendor_categories(
        clusters=_clusters_for(*keys),
        taxonomy=[
            {"name": "Interest and bank charges", "definition": "", "kind": "expense"},
            {"name": "Office supplies", "definition": "", "kind": "expense"},
        ],
        business_profile_text="Name: Example Corp Inc.",
        seed_decisions=None,
        llm_call=lambda _p: response,
    )


_TWO_DECISIONS = [
    {"vendor_key": "wise", "category": "Interest and bank charges",
     "confidence": "high", "reasoning": "Transfer fee."},
    {"vendor_key": "cedarview_supplies", "category": "Office supplies",
     "confidence": "high", "reasoning": "Stationery and printer paper."},
]


def test_phase4_reads_an_object_wrapped_array():
    """What the local model returns: the array lives under a key, because the
    call is made with response_format=json_object and cannot be a bare array."""
    decisions = _decide({"decisions": _TWO_DECISIONS})
    assert [d.category_name for d in decisions] == [
        "Interest and bank charges", "Office supplies",
    ]
    assert not any(d.needs_refinement for d in decisions)


def test_phase4_reads_any_wrapper_key():
    for key in ("vendors", "results", "anything_at_all"):
        decisions = _decide({key: _TWO_DECISIONS})
        assert [d.category_name for d in decisions] == [
            "Interest and bank charges", "Office supplies",
        ], key


def test_phase4_still_reads_a_bare_array():
    """A provider with no response-format constraint does return the array itself."""
    decisions = _decide(_TWO_DECISIONS)
    assert [d.category_name for d in decisions] == [
        "Interest and bank charges", "Office supplies",
    ]


def test_phase4_reads_a_single_decision_object():
    decisions = _decide(_TWO_DECISIONS[0], keys=("wise",))
    assert [d.category_name for d in decisions] == ["Interest and bank charges"]


def test_phase4_matches_vendor_keys_case_and_punctuation_insensitively():
    decisions = _decide({"decisions": [
        {"vendor_key": "Cedarview Supplies", "category": "Office supplies",
         "confidence": "high", "reasoning": "x"},
    ]}, keys=("cedarview_supplies",))
    assert decisions[0].category_name == "Office supplies"
    assert decisions[0].reasoning == "x"


def test_phase4_falls_back_to_position_only_when_nothing_resolves():
    decisions = _decide({"decisions": [
        {"vendor_key": "group-1-item-1", "category": "Interest and bank charges",
         "confidence": "high", "reasoning": "a"},
        {"vendor_key": "group-1-item-2", "category": "Office supplies",
         "confidence": "high", "reasoning": "b"},
    ]})
    assert [d.category_name for d in decisions] == [
        "Interest and bank charges", "Office supplies",
    ]


def test_phase4_never_guesses_position_when_some_keys_did_resolve():
    """A partial reply must leave the unanswered vendors unanswered — shifting
    the remaining entries onto them is how a stationery decision lands on a fee."""
    decisions = _decide({"decisions": [
        {"vendor_key": "cedarview_supplies", "category": "Office supplies",
         "confidence": "high", "reasoning": "b"},
    ]})
    by_key = {d.vendor_key: d for d in decisions}
    assert by_key["cedarview_supplies"].category_name == "Office supplies"
    assert by_key["wise"].category_name is None
    assert by_key["wise"].needs_refinement is True


def test_phase4_logs_the_missing_vendors_and_the_raw_reply(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        _decide({"totally": "unusable"})
    text = caplog.text
    assert "no decision for 2/2 vendors" in text
    assert "wise" in text and "cedarview_supplies" in text
    assert "totally" in text  # the excerpt of what actually came back


def test_reply_excerpt_is_capped():
    from core.categorization_agent.llm_shapes import RAW_EXCERPT_CHARS, excerpt

    text = excerpt({"decisions": [{"reasoning": "x" * 5000}]})
    assert len(text) <= RAW_EXCERPT_CHARS + 1  # +1 for the ellipsis


# ── Phase 5: a batch may only answer for its own transactions ────────────


def test_refinement_drops_ids_that_were_not_in_the_batch(caplog):
    import logging

    from core.categorization_agent.refinement import refine_transactions

    cluster = VendorCluster(
        vendor_key="cedarview_supplies", count=2,
        total_by_currency={"CAD": 20.0},
        date_range=(date(2026, 1, 1), date(2026, 1, 31)),
        sample_descriptions=["CEDARVIEW SUPPLIES LTD"], sample_txn_ids=[1, 2],
        transaction_ids=[1, 2],
    )
    lookup = {
        1: _txn(1, "CEDARVIEW SUPPLIES LTD"),
        2: _txn(2, "CEDARVIEW SUPPLIES LTD"),
    }

    reply = {"refinements": [
        {"id": 1, "category": "Office supplies", "confidence": "high",
         "reasoning": "in batch"},
        {"id": 999, "category": "Travel", "confidence": "high",
         "reasoning": "NOT in batch"},
    ]}

    with caplog.at_level(logging.WARNING):
        overrides = refine_transactions(
            mixed_clusters=[cluster], txn_lookup=lookup,
            taxonomy=[
                {"name": "Office supplies", "definition": "", "kind": "expense"},
                {"name": "Travel", "definition": "", "kind": "expense"},
            ],
            business_profile_text="Name: Example Corp Inc.", llm_call=lambda _p: reply,
        )

    assert overrides[1]["category"] == "Office supplies"
    assert 999 not in overrides
    assert "outside its own batch" in caplog.text


def test_refinement_reads_the_object_wrapped_array():
    from core.categorization_agent.refinement import refine_transactions

    cluster = VendorCluster(
        vendor_key="cedarview_supplies", count=1,
        total_by_currency={"CAD": 10.0},
        date_range=(date(2026, 1, 1), date(2026, 1, 31)),
        sample_descriptions=["CEDARVIEW SUPPLIES LTD"], sample_txn_ids=[1],
        transaction_ids=[1],
    )
    overrides = refine_transactions(
        mixed_clusters=[cluster], txn_lookup={1: _txn(1, "CEDARVIEW SUPPLIES LTD")},
        taxonomy=[{"name": "Office supplies", "definition": "", "kind": "expense"}],
        business_profile_text="Name: Example Corp Inc.",
        llm_call=lambda _p: {"refinements": [
            {"id": 1, "category": "Office supplies", "confidence": "high",
             "reasoning": "r"},
        ]},
    )
    assert overrides[1]["category"] == "Office supplies"


# ── The prompts must ask for what the transport can carry ────────────────


def test_batch_prompts_ask_for_a_json_object_not_a_bare_array():
    """core.llm_sync calls the local endpoint with response_format=json_object,
    which cannot emit a top-level array: a prompt that asks for one gets back
    the first element of the list and nothing else."""
    from core.categorization_agent.prompts import (
        REFINEMENT,
        VENDOR_DECISIONS,
        VENDOR_NORMALIZATION_FALLBACK,
    )

    for template in (VENDOR_DECISIONS, REFINEMENT, VENDOR_NORMALIZATION_FALLBACK):
        assert "JSON OBJECT" in template
        assert "Return ONLY the JSON array" not in template
