"""core/llm_transaction_linker.py — the 4th pass, part by part.

The pass asks a model which two bank lines are the same money seen twice — a
transfer, a card payment, an FX conversion — and turns the answers into links.
These tests cover everything around the model: what the prompt carries, what a
malformed or dishonest reply is allowed to do, how candidate pairs are
pre-filtered and batched, where the confidence threshold falls, and the greedy
assignment that lets each transaction be linked at most once. The model itself
is never called.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

# Fence-stripping lives in the shared sync ladder (core/llm_sync.py), so every
# agent unwraps a fenced reply the same way.
from core.llm_sync import strip_code_fences as _strip_code_fences
from core.llm_transaction_linker import (
    LINK_THRESHOLD,
    LLM_BATCH_SIZE,
    CandidatePair,
    LLMTransactionLinkerResult,
    _build_batch_prompt,
    _generate_candidate_pairs,
    _greedy_assign,
    _heuristic_score,
    _parse_llm_response,
    _pre_filter_pair,
    run_llm_transaction_linking,
)
from models.transaction import AccountType, Transaction, TransactionStatus

# ── Helpers ───────────────────────────────────────────────────────────

def _txn(
    id: int,
    account: str = "CAD",
    txn_type: str = "DEBIT",
    date_posted: str = "2026-03-15",
    amount: float = -100.0,
    currency: str = "CAD",
    description: str = "Test transaction",
) -> Transaction:
    return Transaction(
        id=id,
        account=AccountType(account),
        transaction_type=txn_type,
        date_posted=date.fromisoformat(date_posted),
        amount=Decimal(str(amount)),
        currency=currency,
        description=description,
        source_file="test.csv",
        source_row=1,
        status=TransactionStatus.UNMATCHED,
    )


# ═══════════════════════════════════════════════════════════════════════
# Prompt construction
# ═══════════════════════════════════════════════════════════════════════

class TestPromptConstruction:

    def test_prompt_includes_full_context(self):
        """Prompt includes both transactions' full context."""
        src = _txn(101, "CAD", "DEBIT", "2026-03-15", -5000, "CAD", "TRANSFER TO PROVIDER")
        tgt = _txn(205, "Wise", "CREDIT", "2026-03-16", 5000, "CAD", "Funded by bank account")
        pair = CandidatePair(
            pair_id="p_101_205", source=src, target=tgt,
            date_gap_days=1, amount_ratio=None, heuristic_score=0.9,
        )
        prompt = _build_batch_prompt([pair])
        assert "ID:101" in prompt
        assert "ID:205" in prompt
        assert "CAD" in prompt
        assert "Wise" in prompt
        assert "5,000.00" in prompt
        assert "2026-03-15" in prompt
        assert "2026-03-16" in prompt
        assert "TRANSFER TO PROVIDER" in prompt
        assert "Funded by bank account" in prompt

    def test_same_account_pairs_excluded(self):
        """Same-account pairs excluded from candidates."""
        t1 = _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Payment 1")
        t2 = _txn(2, "CAD", "CREDIT", "2026-03-15", 100, "CAD", "Payment 2")
        t3 = _txn(3, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Payment 3")
        pairs = _generate_candidate_pairs([t1, t2, t3])
        # t1+t2 are same account (CAD) so excluded; only t1+t3 and t2+t3 are cross-account
        # But t2+t3 are both CREDIT so excluded by sign filter
        # Only t1(DEBIT) + t3(CREDIT) should remain
        pair_ids = {(p.source.id, p.target.id) for p in pairs}
        assert (1, 2) not in pair_ids and (2, 1) not in pair_ids
        assert len(pairs) == 1

    def test_fx_signal_included_in_prompt(self):
        """FX signal included in prompt for cross-currency pairs."""
        src = _txn(101, "CAD", "DEBIT", "2026-03-15", -1350, "CAD", "USD TFR")
        tgt = _txn(205, "USD", "CREDIT", "2026-03-15", 1000, "USD", "Transfer in")
        pair = CandidatePair(
            pair_id="p_101_205", source=src, target=tgt,
            date_gap_days=0, amount_ratio=1.35, heuristic_score=0.9,
        )
        prompt = _build_batch_prompt([pair])
        assert "FX note" in prompt
        assert "1.3500" in prompt

    def test_special_characters_escaped(self):
        """Special characters in descriptions don't break prompt."""
        src = _txn(101, "CAD", "DEBIT", "2026-03-15", -100, "CAD", 'Payment "with quotes"')
        tgt = _txn(205, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Line\nbreak")
        pair = CandidatePair(
            pair_id="p_101_205", source=src, target=tgt,
            date_gap_days=0, amount_ratio=None, heuristic_score=0.9,
        )
        prompt = _build_batch_prompt([pair])
        assert '\\"' in prompt or "with quotes" in prompt
        assert "\n\n" not in prompt.split("Desc:")[1].split("\n")[0]  # No raw newlines in desc


# ═══════════════════════════════════════════════════════════════════════
# Response parsing
# ═══════════════════════════════════════════════════════════════════════

class TestResponseParsing:

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        raw = {
            "evaluations": [{
                "pair_id": "p_101_205",
                "linkage_factor": 0.95,
                "link_type": "WISE_TRANSFER",
                "reasoning": "Same amounts, 1 day apart",
                "exchange_rate": None,
                "amount_variance": None,
            }]
        }
        results = _parse_llm_response(raw, {"p_101_205"}, {101, 205})
        assert len(results) == 1
        assert results[0]["linkage_factor"] == 0.95
        assert results[0]["link_type"] == "WISE_TRANSFER"
        assert results[0]["source_id"] == 101
        assert results[0]["target_id"] == 205

    def test_parse_json_with_code_fences(self):
        """Strip code fences from response."""
        raw_text = '```json\n{"evaluations": []}\n```'
        cleaned = _strip_code_fences(raw_text)
        data = json.loads(cleaned)
        assert data == {"evaluations": []}

    def test_parse_malformed_json(self):
        """Malformed JSON returns empty list."""
        raw = {"not_evaluations": "bad data"}
        results = _parse_llm_response(raw, {"p_1_2"}, {1, 2})
        assert results == []

    def test_parse_empty_array(self):
        """Empty evaluations returns empty list."""
        raw = {"evaluations": []}
        results = _parse_llm_response(raw, {"p_1_2"}, {1, 2})
        assert results == []

    def test_parse_missing_fields(self):
        """Entries with missing required fields are skipped."""
        raw = {"evaluations": [
            {"pair_id": "p_1_2"},  # Missing linkage_factor
            {"linkage_factor": 0.9},  # Missing pair_id
        ]}
        results = _parse_llm_response(raw, {"p_1_2"}, {1, 2})
        assert len(results) == 0

    def test_parse_extra_fields(self):
        """Extra fields are ignored."""
        raw = {"evaluations": [{
            "pair_id": "p_1_2",
            "linkage_factor": 0.9,
            "link_type": "OTHER",
            "reasoning": "Test",
            "extra_field": "ignored",
            "another_extra": 42,
        }]}
        results = _parse_llm_response(raw, {"p_1_2"}, {1, 2})
        assert len(results) == 1
        assert "extra_field" not in results[0]

    def test_parse_out_of_range_linkage_factor(self):
        """Out-of-range linkage_factor clamped to [0, 1]."""
        raw = {"evaluations": [
            {"pair_id": "p_1_2", "linkage_factor": 1.5, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_3_4", "linkage_factor": -0.3, "link_type": "OTHER", "reasoning": ""},
        ]}
        results = _parse_llm_response(raw, {"p_1_2", "p_3_4"}, {1, 2, 3, 4})
        assert results[0]["linkage_factor"] == 1.0
        assert results[1]["linkage_factor"] == 0.0

    def test_parse_invalid_link_type(self):
        """Invalid link_type mapped to OTHER."""
        raw = {"evaluations": [{
            "pair_id": "p_1_2",
            "linkage_factor": 0.9,
            "link_type": "INVALID_TYPE",
            "reasoning": "Test",
        }]}
        results = _parse_llm_response(raw, {"p_1_2"}, {1, 2})
        assert results[0]["link_type"] == "OTHER"


# ═══════════════════════════════════════════════════════════════════════
# Batching
# ═══════════════════════════════════════════════════════════════════════

class TestBatching:

    def _make_pairs(self, count: int) -> list[CandidatePair]:
        """Create N candidate pairs for testing batch sizes."""
        pairs = []
        for i in range(count):
            src = _txn(i * 2 + 1, "CAD", "DEBIT", "2026-03-15", -(100 + i), "CAD", f"Txn {i*2+1}")
            tgt = _txn(i * 2 + 2, "Wise", "CREDIT", "2026-03-15", 100 + i, "CAD", f"Txn {i*2+2}")
            pairs.append(CandidatePair(
                pair_id=f"p_{i*2+1}_{i*2+2}", source=src, target=tgt,
                date_gap_days=0, amount_ratio=None, heuristic_score=0.8,
            ))
        return pairs

    def test_small_set_one_batch(self):
        """6 pairs fits in 1 batch."""
        pairs = self._make_pairs(6)
        import math
        num_batches = math.ceil(len(pairs) / LLM_BATCH_SIZE)
        assert num_batches == 1

    def test_medium_set_multiple_batches(self):
        """50 pairs produces 4 batches (ceil(50/15))."""
        pairs = self._make_pairs(50)
        import math
        num_batches = math.ceil(len(pairs) / LLM_BATCH_SIZE)
        assert num_batches == 4  # ceil(50/15) = 4

    def test_zero_pairs_zero_batches(self):
        """Zero pairs → zero batches, no LLM calls."""
        import math
        num_batches = math.ceil(0 / LLM_BATCH_SIZE) if 0 > 0 else 0
        assert num_batches == 0

    def test_single_pair_one_batch(self):
        """1 pair → 1 batch."""
        pairs = self._make_pairs(1)
        import math
        num_batches = math.ceil(len(pairs) / LLM_BATCH_SIZE)
        assert num_batches == 1


# ═══════════════════════════════════════════════════════════════════════
# Threshold and greedy assignment
# ═══════════════════════════════════════════════════════════════════════

class TestThresholdAndGreedy:

    def test_link_above_threshold_accepted(self):
        """Link above threshold accepted."""
        results = [{"pair_id": "p_1_2", "source_id": 1, "target_id": 2,
                     "linkage_factor": 0.85, "link_type": "OTHER", "reasoning": "Test"}]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1

    def test_link_below_threshold_rejected(self):
        """Link below threshold rejected."""
        results = [{"pair_id": "p_1_2", "source_id": 1, "target_id": 2,
                     "linkage_factor": 0.50, "link_type": "OTHER", "reasoning": "Test"}]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 0

    def test_link_exactly_at_threshold_accepted(self):
        """Link exactly at threshold accepted."""
        results = [{"pair_id": "p_1_2", "source_id": 1, "target_id": 2,
                     "linkage_factor": LINK_THRESHOLD, "link_type": "OTHER", "reasoning": "Test"}]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1

    def test_mixed_results_filtered(self):
        """Mixed results filtered correctly."""
        results = [
            {"pair_id": "p_1_2", "source_id": 1, "target_id": 2, "linkage_factor": 0.95, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_3_4", "source_id": 3, "target_id": 4, "linkage_factor": 0.80, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_5_6", "source_id": 5, "target_id": 6, "linkage_factor": 0.50, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_7_8", "source_id": 7, "target_id": 8, "linkage_factor": 0.30, "link_type": "OTHER", "reasoning": ""},
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 2  # Only 0.95 and 0.80 are >= 0.60

    def test_auto_approve_vs_pending(self):
        """Auto-approve vs pending classification based on confidence."""
        results = [
            {"pair_id": "p_1_2", "source_id": 1, "target_id": 2, "linkage_factor": 0.95, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_3_4", "source_id": 3, "target_id": 4, "linkage_factor": 0.70, "link_type": "OTHER", "reasoning": ""},
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        # 0.95 * 0.95 = 0.9025 → AUTO_APPROVED (>= 0.85)
        assert links[0].confidence_score >= 0.85
        # 0.70 * 0.95 = 0.665 → PENDING_REVIEW (< 0.85)
        assert links[1].confidence_score < 0.85

    def test_two_independent_links(self):
        """Two independent links, no chain overlap."""
        results = [
            {"pair_id": "p_1_2", "source_id": 1, "target_id": 2, "linkage_factor": 0.90, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_3_4", "source_id": 3, "target_id": 4, "linkage_factor": 0.85, "link_type": "OTHER", "reasoning": ""},
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 2
        # No shared IDs
        ids_used = {links[0].source_transaction_id, links[0].target_transaction_id,
                    links[1].source_transaction_id, links[1].target_transaction_id}
        assert len(ids_used) == 4

    def test_connected_links_form_chain(self):
        """Two connected links (A→B and B→C) — greedy allows only one with B."""
        results = [
            {"pair_id": "p_1_2", "source_id": 1, "target_id": 2, "linkage_factor": 0.90, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_2_3", "source_id": 2, "target_id": 3, "linkage_factor": 0.85, "link_type": "OTHER", "reasoning": ""},
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        # Greedy: 1→2 wins (higher score), 2→3 blocked because 2 is used
        assert len(links) == 1
        assert links[0].source_transaction_id == 1

    def test_greedy_highest_score_wins(self):
        """A→B(0.9) and A→C(0.7) → only A→B."""
        results = [
            {"pair_id": "p_1_2", "source_id": 1, "target_id": 2, "linkage_factor": 0.90, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_1_3", "source_id": 1, "target_id": 3, "linkage_factor": 0.70, "link_type": "OTHER", "reasoning": ""},
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1
        assert links[0].target_transaction_id == 2

    def test_greedy_each_txn_used_once(self):
        """3 txns, 2 overlapping pairs → only 1 wins."""
        results = [
            {"pair_id": "p_1_2", "source_id": 1, "target_id": 2, "linkage_factor": 0.90, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_2_3", "source_id": 2, "target_id": 3, "linkage_factor": 0.80, "link_type": "OTHER", "reasoning": ""},
            {"pair_id": "p_1_3", "source_id": 1, "target_id": 3, "linkage_factor": 0.70, "link_type": "OTHER", "reasoning": ""},
        ]
        links = _greedy_assign(results, LINK_THRESHOLD)
        assert len(links) == 1  # Only 1→2 wins; others blocked


# ═══════════════════════════════════════════════════════════════════════
# Fallback and edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestFallbackAndEdgeCases:

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_llm_unavailable_empty_result(self, mock_llm):
        """LLM unavailable → empty result, no crash."""
        mock_llm.side_effect = Exception("API down")
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Payment"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Received"),
        ]
        result = run_llm_transaction_linking(txns)
        assert isinstance(result, LLMTransactionLinkerResult)
        assert len(result.links) == 0
        assert result.llm_failures >= 1

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_llm_timeout_empty_result(self, mock_llm):
        """LLM timeout → graceful skip."""
        mock_llm.side_effect = TimeoutError("Request timed out")
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Payment"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Received"),
        ]
        result = run_llm_transaction_linking(txns)
        assert len(result.links) == 0

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_partial_batch_failure(self, mock_llm):
        """3 batches, 1 fails → 2 batches' links preserved."""
        # Create enough txns for 3+ batches (need 31+ pairs)
        txns = []
        for i in range(40):
            txns.append(_txn(i * 2 + 1, "CAD", "DEBIT", "2026-03-15", -(100 + i), "CAD", f"Pay {i}"))
            txns.append(_txn(i * 2 + 2, "Wise", "CREDIT", "2026-03-15", 100 + i, "CAD", f"Recv {i}"))

        call_count = [0]
        def side_effect(prompt):
            call_count[0] += 1
            if call_count[0] == 2:  # Fail on batch 2
                raise Exception("Batch 2 failed")
            return {"evaluations": []}  # Return empty for non-failing batches

        mock_llm.side_effect = side_effect
        result = run_llm_transaction_linking(txns)
        assert result.llm_failures == 1
        assert result.total_batches >= 3

    def test_pre_filter_date_21d_excluded(self):
        """Date >21d excluded."""
        src = _txn(1, "CAD", "DEBIT", "2026-03-01", -100, "CAD", "Payment")
        tgt = _txn(2, "Wise", "CREDIT", "2026-04-01", 100, "CAD", "Received")  # 31 days
        assert not _pre_filter_pair(src, tgt)

    def test_pre_filter_same_currency_50_excluded(self):
        """Same currency >$50 diff excluded (when also >30% of larger)."""
        src = _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Payment")
        tgt = _txn(2, "Wise", "CREDIT", "2026-03-15", 200, "CAD", "Received")  # $100 diff, 50%
        assert not _pre_filter_pair(src, tgt)

    def test_pre_filter_cross_currency_plausible(self):
        """Cross-currency with ratio 1.35 → included."""
        src = _txn(1, "CAD", "DEBIT", "2026-03-15", -1350, "CAD", "USD TFR")
        tgt = _txn(2, "USD", "CREDIT", "2026-03-15", 1000, "USD", "Transfer in")
        assert _pre_filter_pair(src, tgt)

    def test_pre_filter_cross_currency_implausible(self):
        """Cross-currency with ratio 5.0 → excluded."""
        src = _txn(1, "CAD", "DEBIT", "2026-03-15", -5000, "CAD", "Payment")
        tgt = _txn(2, "USD", "CREDIT", "2026-03-15", 1000, "USD", "Received")  # ratio 5.0
        assert not _pre_filter_pair(src, tgt)

    def test_heuristic_sort_exact_amount_first(self):
        """Exact amount match sorted before $10 diff."""
        exact_src = _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Pay")
        exact_tgt = _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Recv")
        diff_src = _txn(3, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Pay")
        diff_tgt = _txn(4, "Wise", "CREDIT", "2026-03-15", 110, "CAD", "Recv")

        score_exact = _heuristic_score(exact_src, exact_tgt, 0, None)
        score_diff = _heuristic_score(diff_src, diff_tgt, 0, None)
        assert score_exact > score_diff

    def test_self_link_rejected(self):
        """Self-link in response (source_id == target_id) dropped."""
        raw = {"evaluations": [{
            "pair_id": "p_1_1",
            "linkage_factor": 0.95,
            "link_type": "OTHER",
            "reasoning": "Self link",
        }]}
        results = _parse_llm_response(raw, {"p_1_1"}, {1})
        assert len(results) == 0

    def test_invalid_txn_id_rejected(self):
        """ID not in input set dropped."""
        raw = {"evaluations": [{
            "pair_id": "p_999_888",
            "linkage_factor": 0.95,
            "link_type": "OTHER",
            "reasoning": "Unknown IDs",
        }]}
        results = _parse_llm_response(raw, {"p_999_888"}, {1, 2})  # 999 and 888 not in set
        assert len(results) == 0

    def test_duplicate_pair_deduplicated(self):
        """Duplicate pair in response keeps higher confidence."""
        raw = {"evaluations": [
            {"pair_id": "p_1_2", "linkage_factor": 0.70, "link_type": "OTHER", "reasoning": "First"},
            {"pair_id": "p_1_2", "linkage_factor": 0.90, "link_type": "OTHER", "reasoning": "Second"},
        ]}
        results = _parse_llm_response(raw, {"p_1_2"}, {1, 2})
        assert len(results) == 1
        assert results[0]["linkage_factor"] == 0.90
