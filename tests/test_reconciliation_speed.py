"""Guards for the three things that make a match run fast without changing it.

Nearly all of a reconciliation run is spent waiting on the LLM server, so the
engine has three ways of asking it fewer questions. Each one is only allowed to
cut *time*:

1. **The amount envelope** — the amount agent is shown only transactions whose
   magnitude is in a band strictly wider than every route the engine can accept
   an amount on. Narrowing must never hide a candidate the deterministic pass
   would have shortlisted.
2. **The deterministic settle** — a document with exactly one obvious candidate
   is settled without asking any agent. The tests below pin every boundary of
   "obvious": one candidate short of two, two cents, three days, the vendor
   score, the merchant contradiction, and same-currency-only.
3. **The verdict memory** — an unchanged question reuses the stored answer, and
   any change to what the prompt renders makes it a different question.

Pure Python: no database, no network, no LLM.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.adjudication_memory import (
    AGENT_AMOUNT,
    AGENT_FINAL,
    AdjudicationMemory,
    inputs_hash,
)
from core.reconciliation import (
    AMOUNT_ENVELOPE_ABS_SLACK,
    AMOUNT_ENVELOPE_HIGH_RATIO,
    AMOUNT_ENVELOPE_LOW_RATIO,
    UNAMBIGUOUS_AMOUNT_TOLERANCE,
    UNAMBIGUOUS_MAX_DATE_GAP_DAYS,
    UNAMBIGUOUS_MIN_VENDOR_SCORE,
    _amount_envelope,
    _deterministic_amount_shortlist,
    _unambiguous_candidate,
    _within_amount_envelope,
    compute_vendor_score,
)
from models.document import Document, DocumentStatus
from models.transaction import Transaction, TransactionStatus


def _txn(
    *,
    id: int = 1,
    account: str = "CAD",
    transaction_type: str = "DEBIT",
    amount: Decimal = Decimal("104.55"),
    currency: str = "CAD",
    description: str = "ORCHID TELECOM",
    date_posted: date = date(2026, 6, 14),
) -> Transaction:
    return Transaction(
        id=id,
        account=account,
        transaction_type=transaction_type,
        date_posted=date_posted,
        amount=amount,
        currency=currency,
        description=description,
        source_file="test.csv",
        source_row=1,
        status=TransactionStatus.UNMATCHED,
    )


def _doc(
    *,
    id: int = 900,
    vendor: str = "Orchid Telecom",
    total: Decimal = Decimal("104.55"),
    currency: str = "CAD",
    document_date: date = date(2026, 6, 14),
    original_filename: str = "20260614-orchid-telecom-receipt.pdf",
) -> Document:
    return Document(
        id=id,
        original_filename=original_filename,
        file_hash="b" * 32,
        vendor=vendor,
        document_date=document_date,
        currency=currency,
        total=total,
        status=DocumentStatus.EXTRACTED,
    )


def _map(*txns: Transaction) -> dict[int, Transaction]:
    return {t.id: t for t in txns}


# ── The amount envelope ───────────────────────────────────────────────────


def test_envelope_brackets_the_document_total():
    doc = _doc(total=Decimal("100.00"))
    (low, high), = _amount_envelope(doc)
    assert low == Decimal("100.00") * AMOUNT_ENVELOPE_LOW_RATIO - AMOUNT_ENVELOPE_ABS_SLACK
    assert high == Decimal("100.00") * AMOUNT_ENVELOPE_HIGH_RATIO + AMOUNT_ENVELOPE_ABS_SLACK


def test_envelope_never_goes_negative_on_a_small_receipt():
    """A $4 coffee's lower bound is 0, not -$28."""
    (low, _high), = _amount_envelope(_doc(total=Decimal("4.00")))
    assert low == Decimal("0")


def test_envelope_keeps_a_cross_currency_pair_at_the_top_of_the_fx_band():
    """USD 3,000 against CAD 4,650 (rate 1.55, the band's ceiling) stays in."""
    doc = _doc(total=Decimal("3000.00"), currency="USD")
    txn = _txn(amount=Decimal("4650.00"), currency="CAD")
    assert _within_amount_envelope(txn, _amount_envelope(doc)) is True


def test_envelope_keeps_a_wire_that_lost_its_fee():
    """A $25 wire fee off a $60 incoming wire is still in the band."""
    doc = _doc(total=Decimal("60.00"))
    assert _within_amount_envelope(_txn(amount=Decimal("35.00")), _amount_envelope(doc)) is True


def test_envelope_drops_an_amount_no_rule_could_accept():
    """A $4,000 bank row against a $104.55 receipt is not a candidate anywhere."""
    doc = _doc(total=Decimal("104.55"))
    assert _within_amount_envelope(_txn(amount=Decimal("4000.00")), _amount_envelope(doc)) is False


def test_envelope_contains_every_deterministically_shortlisted_candidate():
    """The safety property: narrowing can never hide a real candidate.

    One transaction per route the deterministic shortlist has — exact, within
    $2, within 15%, a CAD/USD conversion — all of which must be inside the
    envelope, because the envelope is what decides whether the model sees them.
    """
    doc = _doc(total=Decimal("100.00"), currency="USD")
    candidates = [
        _txn(id=1, amount=Decimal("100.00"), currency="USD"),   # exact
        _txn(id=2, amount=Decimal("101.50"), currency="USD"),   # within $2
        _txn(id=3, amount=Decimal("113.00"), currency="USD"),   # within 15%
        _txn(id=4, amount=Decimal("135.00"), currency="CAD"),   # FX 1.35
        _txn(id=5, amount=Decimal("152.00"), currency="CAD"),   # FX 1.52
    ]
    shortlist = _deterministic_amount_shortlist(doc, candidates)
    assert shortlist, "fixture is wrong if the deterministic pass finds nothing"
    bands = _amount_envelope(doc)
    by_id = _map(*candidates)
    for txn_id in shortlist:
        assert _within_amount_envelope(by_id[txn_id], bands), (
            f"txn {txn_id} was shortlisted but the envelope would have hidden it"
        )


def test_envelope_is_empty_for_a_document_with_no_total():
    doc = _doc(total=Decimal("0.00"))
    assert _amount_envelope(doc) == []
    # An empty band list means "no narrowing possible", so everything passes.
    assert _within_amount_envelope(_txn(amount=Decimal("999999")), []) is True


# ── The deterministic settle: every boundary ──────────────────────────────


def test_settles_an_obvious_pair_without_an_agent():
    txn = _txn()
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is txn


def test_two_candidates_are_never_unambiguous():
    a, b = _txn(id=1), _txn(id=2, amount=Decimal("104.55"))
    assert _unambiguous_candidate(_doc(), [a.id, b.id], _map(a, b), 45) is None


def test_no_candidate_is_never_unambiguous():
    assert _unambiguous_candidate(_doc(), [], {}, 45) is None


def test_amount_at_the_tolerance_still_settles():
    txn = _txn(amount=Decimal("104.55") + UNAMBIGUOUS_AMOUNT_TOLERANCE)
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is txn


def test_amount_one_cent_past_the_tolerance_goes_to_the_agents():
    txn = _txn(
        amount=Decimal("104.55") + UNAMBIGUOUS_AMOUNT_TOLERANCE + Decimal("0.01"),
    )
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is None


def test_a_near_miss_amount_goes_to_the_agents():
    """$1 apart is a good candidate; it is not an unambiguous one."""
    txn = _txn(amount=Decimal("119.72"))
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is None


def test_date_gap_at_the_limit_still_settles():
    txn = _txn(date_posted=date(2026, 6, 14) + __import__("datetime").timedelta(
        days=UNAMBIGUOUS_MAX_DATE_GAP_DAYS))
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is txn


def test_date_gap_one_day_past_the_limit_goes_to_the_agents():
    txn = _txn(date_posted=date(2026, 6, 14) + __import__("datetime").timedelta(
        days=UNAMBIGUOUS_MAX_DATE_GAP_DAYS + 1))
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is None


def test_the_run_wide_date_cutoff_still_wins_when_it_is_tighter():
    """max_date_days=1 overrides the settle rule's own 3-day band."""
    txn = _txn(date_posted=date(2026, 6, 16))
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is txn
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 1) is None


def test_a_document_with_no_usable_date_goes_to_the_agents():
    doc = _doc(document_date=None, original_filename="scan001.pdf")
    txn = _txn()
    assert _unambiguous_candidate(doc, [txn.id], _map(txn), 45) is None


def test_cross_currency_is_never_settled_deterministically():
    """Even at a perfectly plausible rate, FX goes to the agents."""
    doc = _doc(total=Decimal("100.00"), currency="USD")
    txn = _txn(amount=Decimal("100.00"), currency="CAD")
    assert _unambiguous_candidate(doc, [txn.id], _map(txn), 45) is None


def test_a_missing_currency_on_either_side_goes_to_the_agents():
    doc = _doc(currency=None)
    txn = _txn()
    assert _unambiguous_candidate(doc, [txn.id], _map(txn), 45) is None


def test_vendor_below_the_strong_signal_goes_to_the_agents():
    """An opaque bank line names nobody; that is a question, not an answer."""
    txn = _txn(description="POS PURCHASE 0000")
    assert compute_vendor_score(
        txn.description, "Orchid Telecom", "") < UNAMBIGUOUS_MIN_VENDOR_SCORE
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is None


def test_vendor_at_the_strong_signal_settles():
    txn = _txn(description="ORCHID TELECOM")
    assert compute_vendor_score(
        txn.description, "Orchid Telecom", "") >= UNAMBIGUOUS_MIN_VENDOR_SCORE
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is txn


def test_a_contradicting_merchant_is_never_settled():
    """A bank line naming a different merchant is never settled without asking.

    Amount and date are exact and there is only one candidate, so every other
    part of "obvious" is satisfied: the merchant on the line is the only thing
    that can stop the pair, and it must. Both gates are asserted — the vendor
    score stays under the strong-signal bar, and the settle returns nothing —
    so loosening either one on its own fails this test.
    """
    txn = _txn(description="CEDARVIEW SUPPLIES #0000")
    assert compute_vendor_score(
        txn.description, "Orchid Telecom", "") < UNAMBIGUOUS_MIN_VENDOR_SCORE
    assert _unambiguous_candidate(_doc(), [txn.id], _map(txn), 45) is None


def test_a_payment_rail_line_is_not_an_unambiguous_pair():
    """"WISE TRANSFER" names the pipe, not the payee — the agents decide."""
    txn = _txn(description="WISE TRANSFER OUTGOING")
    assert _unambiguous_candidate(
        _doc(vendor="Jordan Avery Consulting",
             original_filename="jordan-avery-invoice.pdf"),
        [txn.id], _map(txn), 45,
    ) is None


def test_a_shortlisted_id_that_is_not_in_the_map_is_not_settled():
    assert _unambiguous_candidate(_doc(), [4], {}, 45) is None


# ── The verdict memory: reuse and invalidation ────────────────────────────


def _digest(doc: Document, txns: list[Transaction], model: str = "primary") -> str:
    return inputs_hash(AGENT_AMOUNT, doc, txns, model)


def test_an_unchanged_question_is_reused():
    doc, txn = _doc(), _txn()
    digest = _digest(doc, [txn])
    memory = AdjudicationMemory()
    memory.put(AGENT_AMOUNT, doc.id, digest, {"shortlist": [txn.id]}, "primary")
    assert memory.get(AGENT_AMOUNT, doc.id, _digest(doc, [txn])) == {"shortlist": [txn.id]}
    assert memory.hits == 1


def test_a_question_never_asked_is_a_miss():
    doc, txn = _doc(), _txn()
    memory = AdjudicationMemory()
    assert memory.get(AGENT_AMOUNT, doc.id, _digest(doc, [txn])) is None
    assert memory.misses == 1


def test_editing_the_document_total_invalidates_the_verdict():
    txn = _txn()
    before = _digest(_doc(total=Decimal("104.55")), [txn])
    after = _digest(_doc(total=Decimal("120.00")), [txn])
    assert before != after


def test_editing_the_document_vendor_invalidates_the_verdict():
    txn = _txn()
    assert _digest(_doc(vendor="Orchid Telecom"), [txn]) != _digest(
        _doc(vendor="Cedarview Supplies Ltd"), [txn]
    )


def test_editing_the_document_date_invalidates_the_verdict():
    txn = _txn()
    assert _digest(_doc(document_date=date(2026, 6, 14)), [txn]) != _digest(
        _doc(document_date=date(2026, 6, 15)), [txn]
    )


def test_editing_a_transaction_amount_invalidates_the_verdict():
    doc = _doc()
    assert _digest(doc, [_txn(amount=Decimal("104.55"))]) != _digest(
        doc, [_txn(amount=Decimal("118.73"))]
    )


def test_editing_a_transaction_description_invalidates_the_verdict():
    doc = _doc()
    assert _digest(doc, [_txn(description="ORCHID TELECOM")]) != _digest(
        doc, [_txn(description="ORCHID TELECOM PREAUTH PMT")]
    )


def test_a_description_edit_past_what_the_prompt_shows_does_not_invalidate():
    """The prompt truncates at 80 characters; an edit past it changed nothing."""
    doc = _doc()
    base = "T" * 80
    assert _digest(doc, [_txn(description=base + "AAA")]) == _digest(
        doc, [_txn(description=base + "ZZZ")]
    )


def test_adding_a_candidate_invalidates_the_verdict():
    doc = _doc()
    one = [_txn(id=1)]
    two = [_txn(id=1), _txn(id=2, amount=Decimal("118.00"))]
    assert _digest(doc, one) != _digest(doc, two)


def test_reordering_candidates_invalidates_the_verdict():
    """Order is part of the question a language model was asked."""
    doc = _doc()
    a, b = _txn(id=1), _txn(id=2, amount=Decimal("118.00"))
    assert _digest(doc, [a, b]) != _digest(doc, [b, a])


def test_changing_the_model_invalidates_the_verdict():
    doc, txn = _doc(), _txn()
    assert _digest(doc, [txn], "primary") != _digest(doc, [txn], "some-other-model")


def test_the_date_agents_max_days_is_part_of_the_question():
    from core.adjudication_memory import AGENT_DATE

    doc, txn = _doc(), _txn()
    assert inputs_hash(AGENT_DATE, doc, [txn], "primary", {"max_date_days": 45}) != (
        inputs_hash(AGENT_DATE, doc, [txn], "primary", {"max_date_days": 30})
    )


def test_each_agent_asks_its_own_question():
    doc, txn = _doc(), _txn()
    assert inputs_hash(AGENT_AMOUNT, doc, [txn], "primary") != inputs_hash(
        AGENT_FINAL, doc, [txn], "primary"
    )


def test_a_verdict_is_queued_for_persisting_exactly_once():
    doc, txn = _doc(), _txn()
    digest = _digest(doc, [txn])
    memory = AdjudicationMemory()
    memory.put(AGENT_FINAL, doc.id, digest, {"matched_transaction_id": txn.id},
               "primary", transaction_id=txn.id, confidence=0.9, reasoning="same vendor")
    memory.put(AGENT_FINAL, doc.id, digest, {"matched_transaction_id": txn.id}, "primary")
    pending = memory.pending()
    assert len(pending) == 1
    assert pending[0]["transaction_id"] == txn.id
    assert pending[0]["confidence"] == 0.9
    assert pending[0]["agent"] == AGENT_FINAL


def test_a_loaded_row_is_reused_without_being_written_again():
    doc, txn = _doc(), _txn()
    digest = _digest(doc, [txn])
    memory = AdjudicationMemory([{
        "document_id": doc.id,
        "transaction_id": None,
        "agent": AGENT_AMOUNT,
        "inputs_hash": digest,
        "verdict": {"shortlist": [txn.id]},
    }])
    assert memory.get(AGENT_AMOUNT, doc.id, digest) == {"shortlist": [txn.id]}
    assert memory.pending() == []


def test_a_row_stored_as_a_json_string_is_still_usable():
    """psycopg2 returns jsonb as a dict; a text column would give a string."""
    doc, txn = _doc(), _txn()
    digest = _digest(doc, [txn])
    memory = AdjudicationMemory([{
        "document_id": doc.id,
        "agent": AGENT_AMOUNT,
        "inputs_hash": digest,
        "verdict": '{"shortlist": [1]}',
    }])
    assert memory.get(AGENT_AMOUNT, doc.id, digest) == {"shortlist": [1]}


def test_a_disabled_memory_never_hits_and_never_stores():
    from core.adjudication_memory import DISABLED

    doc, txn = _doc(), _txn()
    digest = _digest(doc, [txn])
    DISABLED.put(AGENT_AMOUNT, doc.id, digest, {"shortlist": []}, "primary")
    assert DISABLED.get(AGENT_AMOUNT, doc.id, digest) is None
    assert DISABLED.pending() == []


# ── The 4th-pass linker runs batches concurrently, in batch order ─────────


def test_the_linker_reassembles_batches_in_order_whatever_finishes_first():
    """Concurrency must not reorder the evaluations the greedy pass scores.

    ``_greedy_assign`` sorts by linkage_factor with Python's stable sort, so two
    pairs at the same factor are decided by their position in the list. The
    batches run several at a time; the stub below answers the *last* batch
    fastest, and the assembled list must still be batch 1, batch 2, ... exactly
    as a serial loop would produce it.
    """
    import re
    import time
    from unittest.mock import patch

    import core.llm_transaction_linker as linker

    txns = []
    for i in range(40):
        txns.append(_link_txn(i * 2 + 1, "CAD", "DEBIT", -(100 + i), f"Pay {i}"))
        txns.append(_link_txn(i * 2 + 2, "Wise", "CREDIT", (100 + i), f"Recv {i}"))

    finished_order: list[str] = []
    call_index = [0]

    def answer_later_batches_sooner(prompt: str) -> dict:
        # Every batch is tagged by the first pair id its prompt carries.
        tag = re.search(r"--- Pair (\S+) ---", prompt).group(1)
        # The earlier a batch was dispatched, the longer it takes, so the
        # completion order is the reverse of the batch order.
        index = call_index[0]
        call_index[0] += 1
        time.sleep(max(0.0, (8 - index) * 0.02))
        finished_order.append(tag)
        return {"__tag__": tag}

    def one_evaluation_per_batch(raw, *_args, **_kwargs):
        return [{
            "pair_id": raw["__tag__"], "source_id": 1, "target_id": 2,
            "link_type": "INTERNAL_TRANSFER", "linkage_factor": 0.9,
            "reasoning": raw["__tag__"],
        }]

    captured: list[list[dict]] = []

    def capture_then_assign(all_results, *args, **kwargs):
        captured.append(list(all_results))
        return []

    with patch.object(linker, "_call_llm_for_linking", side_effect=answer_later_batches_sooner), \
            patch.object(linker, "_parse_llm_response", side_effect=one_evaluation_per_batch), \
            patch.object(linker, "_greedy_assign", side_effect=capture_then_assign):
        result = linker.run_llm_transaction_linking(txns)

    # What batch order actually is, derived the same way the linker derives it.
    candidates = linker._generate_candidate_pairs(txns, None)
    candidates.sort(key=lambda c: c.heuristic_score, reverse=True)
    candidates = candidates[:linker.MAX_CANDIDATES_TOTAL]
    expected = [
        candidates[i].pair_id
        for i in range(0, len(candidates), linker.LLM_BATCH_SIZE)
    ]

    assert result.total_batches >= 3
    assert result.llm_failures == 0
    scored = [r["pair_id"] for r in captured[0]]
    # The stub deliberately finished the batches out of order...
    assert finished_order != expected, "stub did not actually reorder completions"
    # ...and the list handed to the greedy pass is still in batch order.
    assert scored == expected


def test_the_linker_bounds_how_many_batches_are_in_flight():
    """Bounded, and deliberately below the matcher's bound.

    A linking batch decodes 15 verdicts against a 4096-token budget, so it is a
    far heavier call than a matcher question. Push enough of them at a server at
    once and one times out — and a timed-out batch takes all fifteen of its
    candidate pairs down with it, so a link the user should have seen is quietly
    never found. Speed does not get to cost a link.
    """
    from config.settings import local_llm_config
    from core.llm_transaction_linker import MAX_CONCURRENT_BATCHES

    assert MAX_CONCURRENT_BATCHES >= 1
    assert MAX_CONCURRENT_BATCHES < local_llm_config.engine_max_concurrent


def _link_txn(txn_id: int, account: str, kind: str, amount: float, desc: str) -> Transaction:
    return Transaction(
        id=txn_id, account=account, transaction_type=kind,
        date_posted=date(2026, 3, 15), amount=Decimal(str(amount)),
        currency="CAD", description=desc, source_file="s.csv", source_row=txn_id,
        status=TransactionStatus.UNMATCHED,
    )


# ── The progress countdown ────────────────────────────────────────────────


def test_the_eta_reports_nothing_until_it_has_enough_completions():
    from server.api.reconciliation import _EtaTracker

    eta = _EtaTracker()
    eta.begin("documents", 100)
    assert eta.snapshot() == {"pairs_total": 100, "pairs_done": 0, "eta_seconds": None}
    eta.complete()
    assert eta.snapshot()["eta_seconds"] is None, "one sample is not a rate"


def test_the_eta_extrapolates_from_the_rolling_rate():
    import time

    from server.api.reconciliation import _EtaTracker

    eta = _EtaTracker()
    eta.begin("documents", 100)
    for _ in range(4):
        time.sleep(0.02)
        eta.complete()
    snap = eta.snapshot()
    assert snap["pairs_done"] == 4
    # ~0.02s per document, 96 left: a small positive number of seconds.
    assert snap["eta_seconds"] is not None
    assert 0 <= snap["eta_seconds"] <= 30


def test_the_eta_counts_units_not_callbacks():
    """A phase that advances 15 at a time must not be timed per callback."""
    import time

    from server.api.reconciliation import _EtaTracker

    batch, batches, total = 5, 4, 200
    per_unit = _EtaTracker()
    per_unit.begin("pairs", total)
    per_batch = _EtaTracker()
    per_batch.begin("pairs", total)
    for i in range(1, batches + 1):
        for _ in range(batch):
            time.sleep(0.01)
            per_unit.complete()
        per_batch.set_done(i * batch)
    a = per_unit.snapshot()["eta_seconds"]
    b = per_batch.snapshot()["eta_seconds"]
    assert a is not None and b is not None
    # Both saw 20 units in the same wall time, so both must extrapolate to
    # roughly the same remaining seconds — a per-callback rate would make the
    # batched tracker look 5x faster.
    assert abs(a - b) <= max(1, 0.25 * a), (a, b)


def test_the_eta_stops_once_the_phase_is_finished():
    import time

    from server.api.reconciliation import _EtaTracker

    eta = _EtaTracker()
    eta.begin("documents", 3)
    for _ in range(3):
        time.sleep(0.01)
        eta.complete()
    snap = eta.snapshot()
    assert snap["pairs_done"] == 3
    assert snap["eta_seconds"] is None


def test_the_progress_payload_carries_the_countdown_fields():
    from server.api.reconciliation import (
        _EtaTracker,
        _reconciliation_progress,
        _update_progress,
    )

    uid = "test-uuid-progress-countdown"
    try:
        eta = _EtaTracker()
        eta.begin("documents", 149)
        eta.complete(7)
        _update_progress(uid, "3-agent reconciliation running...", percent=20, eta=eta)
        payload = _reconciliation_progress[uid]
        assert payload["pairs_total"] == 149
        assert payload["pairs_done"] == 7
        assert "eta_seconds" in payload
        assert "llm_calls_done" in payload

        # A phase with no tracker carries the last known counts forward rather
        # than blanking the panel mid-run.
        _update_progress(uid, "Storing results...", percent=85)
        carried = _reconciliation_progress[uid]
        assert carried["pairs_total"] == 149
        assert carried["pairs_done"] == 7
    finally:
        _reconciliation_progress.pop(uid, None)


def test_beginning_a_new_phase_forgets_the_old_one():
    from server.api.reconciliation import _EtaTracker

    eta = _EtaTracker()
    eta.begin("documents", 100)
    eta.complete(50)
    eta.begin("candidate pairs", 200)
    assert eta.snapshot() == {"pairs_total": 200, "pairs_done": 0, "eta_seconds": None}


def test_set_done_never_goes_backwards():
    from server.api.reconciliation import _EtaTracker

    eta = _EtaTracker()
    eta.begin("pairs", 100)
    eta.set_done(30)
    eta.set_done(10)
    assert eta.snapshot()["pairs_done"] == 30


def test_a_malformed_row_is_ignored_rather_than_crashing_the_run():
    memory = AdjudicationMemory([
        {"document_id": "not-an-int", "agent": AGENT_AMOUNT, "inputs_hash": "x",
         "verdict": {}},
        {"agent": AGENT_AMOUNT, "inputs_hash": "y", "verdict": {}},
        {"document_id": 1, "agent": AGENT_AMOUNT, "inputs_hash": "z",
         "verdict": "not json at all"},
    ])
    assert memory.stats()["known"] == 0


# ── The amount agent's model call is off by default ───────────────────────
#
# Asking a language model which amounts are close is the slowest half of the
# matching phase, and the candidates it adds beyond the deterministic pass are
# the ones arithmetic already rejected. Pass 1 is a union, so switching the call
# off can only remove candidates the model alone proposed — never one the
# deterministic shortlist found.


def test_the_amount_pass_asks_no_model_when_the_flag_is_off(monkeypatch):
    from core import reconciliation as recon

    monkeypatch.setattr(recon, "AMOUNT_AGENT_LLM", False)

    def _never(*args, **kwargs):
        raise AssertionError("the amount agent called the model with the flag off")

    monkeypatch.setattr(recon, "_call_llm_for_reconciliation", _never)

    txn = _txn(id=11)
    doc = _doc()
    assert recon._agent_monetary_match(doc, [txn]) == [txn.id]


def test_the_flag_on_still_merges_what_the_model_says(monkeypatch):
    """With the flag on the pass is a union: the model adds, it never removes."""
    from core import reconciliation as recon

    monkeypatch.setattr(recon, "AMOUNT_AGENT_LLM", True)
    other = _txn(id=12, amount=Decimal("118.00"), description="SOMETHING ELSE")
    monkeypatch.setattr(
        recon, "_call_llm_for_reconciliation",
        lambda prompt, label="": {"shortlist": [12], "reasoning": "close enough"},
    )

    exact = _txn(id=11)
    merged = recon._agent_monetary_match(_doc(), [exact, other])
    assert sorted(merged) == [11, 12]


def test_the_flag_defaults_to_off():
    from config.settings import ReconciliationConfig

    assert ReconciliationConfig().amount_agent_llm is False


# ── The reasoning is one sentence, and the budget says so ─────────────────


def test_every_agent_prompt_bounds_the_reasoning():
    from core.reconciliation import (
        _AMOUNT_AGENT_PROMPT,
        _DATE_AGENT_PROMPT,
        _FINAL_AGENT_PROMPT,
    )

    for prompt in (_AMOUNT_AGENT_PROMPT, _DATE_AGENT_PROMPT, _FINAL_AGENT_PROMPT):
        assert "25 WORDS" in prompt
        assert "25 words maximum" in prompt


def test_the_decision_fields_come_before_the_reasoning():
    """A truncated reply must still carry the answer."""
    from core.reconciliation import (
        _AMOUNT_AGENT_PROMPT,
        _DATE_AGENT_PROMPT,
        _FINAL_AGENT_PROMPT,
    )

    for prompt, decision in (
        (_AMOUNT_AGENT_PROMPT, '"shortlist"'),
        (_DATE_AGENT_PROMPT, '"shortlist"'),
        (_FINAL_AGENT_PROMPT, '"matched_transaction_id"'),
    ):
        shapes = [
            line for line in prompt.splitlines()
            if line.startswith('{{"') and '"reasoning"' in line
        ]
        assert shapes
        for line in shapes:
            assert line.index(decision) < line.index('"reasoning"'), line


def test_the_matcher_asks_for_a_smaller_budget_than_the_rest_of_the_engine():
    from core.match_validator import ENGINE_JSON_MAX_TOKENS
    from core.reconciliation import RECON_AGENT_MAX_TOKENS

    assert RECON_AGENT_MAX_TOKENS < ENGINE_JSON_MAX_TOKENS
    # Room for a 25-word sentence (~35 tokens), the JSON around it, and a
    # shortlist of dozens of ids.
    assert RECON_AGENT_MAX_TOKENS >= 256


def test_the_budget_reaches_the_ladder(monkeypatch):
    from core import match_validator, reconciliation

    seen = {}
    monkeypatch.setattr(
        "core.llm_sync.call_json",
        lambda prompt, max_tokens, label: seen.update(
            max_tokens=max_tokens, label=label) or {},
    )
    reconciliation._call_llm_for_reconciliation("prompt", label="date_agent")
    assert seen == {
        "max_tokens": reconciliation.RECON_AGENT_MAX_TOKENS,
        "label": "date_agent",
    }

    # Everything else keeps the engine-wide ceiling.
    match_validator._call_llm("prompt", label="match_validator.date_gap")
    assert seen["max_tokens"] == match_validator.ENGINE_JSON_MAX_TOKENS


# ── The verdict memory invalidates when the instructions change ───────────


def test_the_prompt_revision_matches_the_current_instructions():
    from core.adjudication_memory import PROMPT_REVISION

    assert PROMPT_REVISION == "2"


def test_a_verdict_from_a_previous_revision_is_not_found(monkeypatch):
    import core.adjudication_memory as mem

    doc, txns = _doc(), [_txn()]
    old_digest = inputs_hash(AGENT_FINAL, doc, txns, "primary")
    monkeypatch.setattr(mem, "PROMPT_REVISION", "1")
    assert inputs_hash(AGENT_FINAL, doc, txns, "primary") != old_digest
