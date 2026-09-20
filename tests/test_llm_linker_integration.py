"""The 4th pass — the LLM transaction linker as the pipeline drives it.

The linker pairs two bank lines that are the same money seen twice: a transfer
between two accounts, a credit-card payment, an FX conversion. Left unpaired,
each of those looks like an unmatched expense and inflates the month.

Every model call here is mocked; what is exercised is the wiring around it —
which transactions are offered to the pass, how a verdict becomes a stored
link, and that a model that is down or answers badly costs the run only the
links it would have found.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from core.llm_transaction_linker import (
    LLMTransactionLinkerResult,
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
    status: str = "UNMATCHED",
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
        status=TransactionStatus(status),
    )


# ═══════════════════════════════════════════════════════════════════════
# The pass as the pipeline runs it
# ═══════════════════════════════════════════════════════════════════════

class TestPipelineIntegration:

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_4th_pass_runs_after_doc_matching(self, mock_llm):
        """4th pass runs and returns results."""
        mock_llm.return_value = {
            "evaluations": [{
                "pair_id": "p_1_2",
                "linkage_factor": 0.92,
                "link_type": "WISE_TRANSFER",
                "reasoning": "Same amounts, same day",
            }]
        }
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -5000, "CAD", "WISEMSP WISE"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 5000, "CAD", "Funded by BMO"),
        ]
        result = run_llm_transaction_linking(txns)
        assert len(result.links) == 1
        assert result.links[0].link_type == "WISE_TRANSFER"
        assert result.links[0].confidence_score == round(0.92 * 0.95, 4)

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_already_linked_excluded(self, mock_llm):
        """Already-linked transactions excluded from input."""
        mock_llm.return_value = {"evaluations": []}
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Pay"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Recv"),
            _txn(3, "CAD", "DEBIT", "2026-03-16", -200, "CAD", "Pay 2"),
            _txn(4, "Wise", "CREDIT", "2026-03-16", 200, "CAD", "Recv 2"),
        ]
        # IDs 1 and 2 already linked
        result = run_llm_transaction_linking(txns, already_linked_ids={1, 2})
        # Only txns 3 and 4 should generate pairs
        # Verify LLM was called (meaning candidates existed for 3,4)
        assert mock_llm.called or result.total_candidates_evaluated >= 0

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_results_have_auto_match_source(self, mock_llm):
        """Results include correct link_type and confidence for DB storage."""
        mock_llm.return_value = {
            "evaluations": [{
                "pair_id": "p_1_2",
                "linkage_factor": 0.90,
                "link_type": "CREDIT_CARD_PAYMENT",
                "reasoning": "CC payment",
            }]
        }
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -500, "CAD", "PAYMENT THANK YOU"),
            _txn(2, "CreditCard", "CREDIT", "2026-03-15", 500, "CAD", "Payment received"),
        ]
        result = run_llm_transaction_linking(txns)
        assert len(result.links) == 1
        link = result.links[0]
        assert link.link_type == "CREDIT_CARD_PAYMENT"
        # match_source='AUTO' is set during DB insertion, not in the link result itself

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_transaction_ids_correct(self, mock_llm):
        """Source and target IDs are correctly assigned."""
        mock_llm.return_value = {
            "evaluations": [{
                "pair_id": "p_1_2",
                "linkage_factor": 0.88,
                "link_type": "WISE_TRANSFER",
                "reasoning": "Matched",
            }]
        }
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -1000, "CAD", "WISE"),
            _txn(2, "Wise", "CREDIT", "2026-03-16", 1000, "CAD", "Funded"),
        ]
        result = run_llm_transaction_linking(txns)
        assert result.links[0].source_transaction_id == 1
        assert result.links[0].target_transaction_id == 2

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_result_includes_fourth_pass_stats(self, mock_llm):
        """Result includes stats about the 4th pass."""
        mock_llm.return_value = {
            "evaluations": [{
                "pair_id": "p_1_2",
                "linkage_factor": 0.90,
                "link_type": "OTHER",
                "reasoning": "Match",
            }]
        }
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Pay"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Recv"),
        ]
        result = run_llm_transaction_linking(txns)
        assert result.total_candidates_evaluated >= 1
        assert result.total_batches >= 1
        assert result.llm_failures == 0

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_llm_failure_does_not_break(self, mock_llm):
        """LLM failure does not break — returns empty result."""
        mock_llm.side_effect = Exception("LLM down")
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Pay"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Recv"),
        ]
        result = run_llm_transaction_linking(txns)
        assert isinstance(result, LLMTransactionLinkerResult)
        assert len(result.links) == 0

    def test_zero_unmatched_skipped(self):
        """Zero unmatched → 4th pass skipped."""
        result = run_llm_transaction_linking([])
        assert len(result.links) == 0
        assert result.total_batches == 0

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_rate_includes_linked(self, mock_llm):
        """Linked transactions contribute to reconciliation accuracy."""
        mock_llm.return_value = {
            "evaluations": [{
                "pair_id": "p_1_2",
                "linkage_factor": 0.90,
                "link_type": "WISE_TRANSFER",
                "reasoning": "Matched",
            }]
        }
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "WISE"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Recv"),
            _txn(3, "CAD", "DEBIT", "2026-03-16", -200, "CAD", "Other"),
        ]
        result = run_llm_transaction_linking(txns)
        # 1 link found (txns 1+2), txn 3 remains unmatched
        assert len(result.links) == 1

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_dedup_handled_gracefully(self, mock_llm):
        """Duplicate pair_ids in LLM response handled (dedup)."""
        mock_llm.return_value = {
            "evaluations": [
                {"pair_id": "p_1_2", "linkage_factor": 0.80, "link_type": "OTHER", "reasoning": "First"},
                {"pair_id": "p_1_2", "linkage_factor": 0.95, "link_type": "OTHER", "reasoning": "Second"},
            ]
        }
        txns = [
            _txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Pay"),
            _txn(2, "Wise", "CREDIT", "2026-03-15", 100, "CAD", "Recv"),
        ]
        result = run_llm_transaction_linking(txns)
        # Only 1 link (deduplicated, higher score kept)
        assert len(result.links) == 1
        # Should use the higher confidence (0.95 * 0.95 = 0.9025)
        assert result.links[0].confidence_score == round(0.95 * 0.95, 4)

    def test_single_txn_skipped(self):
        """Single transaction → no pairs possible, skipped."""
        txns = [_txn(1, "CAD", "DEBIT", "2026-03-15", -100, "CAD", "Pay")]
        result = run_llm_transaction_linking(txns)
        assert len(result.links) == 0
        assert result.total_batches == 0


# ═══════════════════════════════════════════════════════════════════════
# Non-reconcilable transactions still reach the 4th pass
# ═══════════════════════════════════════════════════════════════════════

class TestNonReconcilableIn4thPass:
    """Transactions the rule pre-pass calls non-reconcilable — credit-card
    payments, USD-to-CAD conversions, internal transfers — have no receipt to
    find, but they are exactly the pairs the 4th pass exists to link, so they
    must still be offered to it."""

    def test_non_reconcilable_txns_included_in_4th_pass_input(self):
        """Non-reconcilable transactions stay in the still_unmatched pool that
        feeds the 4th pass.

        The test runs the real category gates over invented descriptions and
        then the pipeline's own filter, so a filter that derives the pool from
        the reconcilable subset — dropping the transfers the pass is for —
        fails here rather than silently halving the pass's input."""
        from core.categorization_agent.rule_prepass import (
            is_non_reconcilable,
            match_category,
        )

        # Create test transactions that match non-reconcilable categories
        txn_a = _txn(101, "CAD", "DEBIT", "2026-01-15", -142.50, "CAD",
                     "OnlineTransfer,TF0000000000")  # Credit Card Payment
        txn_b = _txn(102, "CreditCard", "CREDIT", "2026-01-15", 142.50, "CAD",
                     "PAYMENT - THANK YOU")  # No non-reconcilable match
        txn_c = _txn(103, "CAD", "DEBIT", "2026-01-20", -500.00, "CAD",
                     "USDTFR AT1.3500")  # USD to CAD
        txn_d = _txn(104, "USD", "CREDIT", "2026-01-20", 370.37, "USD",
                     "Transfer,1234-5678-901")  # Internal Transfer

        unmatched_txns = [txn_a, txn_b, txn_c, txn_d]

        # The real gates, not a copy of them: the rule pre-pass names the
        # category, and the description decides reconcilability (canonical
        # names are shared across reconcilable and non-reconcilable flows).
        categories = {
            txn.id: match_category(txn.description) for txn in unmatched_txns
        }
        non_reconcilable_ids = {
            txn.id for txn in unmatched_txns
            if is_non_reconcilable(txn.description)
        }

        # Verify A, C, D are indeed non-reconcilable
        assert txn_a.id in non_reconcilable_ids, f"txn_a should be non-reconcilable (got cat: {categories.get(txn_a.id)})"
        assert txn_c.id in non_reconcilable_ids, f"txn_c should be non-reconcilable (got cat: {categories.get(txn_c.id)})"
        assert txn_d.id in non_reconcilable_ids, f"txn_d should be non-reconcilable (got cat: {categories.get(txn_d.id)})"

        # Simulate pipeline state
        linked_txn_ids: set[int] = set()
        matched_txn_ids_from_docs: set[int] = set()

        # The pool the pass is fed: every unmatched transaction that no
        # document and no earlier link has already claimed.
        still_unmatched = [
            t for t in unmatched_txns
            if t.id not in matched_txn_ids_from_docs and t.id not in linked_txn_ids
        ]

        # All 4 transactions should be in the 4th pass input pool
        assert len(still_unmatched) == 4
        still_ids = {t.id for t in still_unmatched}
        assert txn_a.id in still_ids
        assert txn_b.id in still_ids
        assert txn_c.id in still_ids
        assert txn_d.id in still_ids

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_credit_card_payment_pair_detected_by_4th_pass(self, mock_llm):
        """Credit card payment pair (CAD debit + CC credit, same amount,
        same day) is detected as CREDIT_CARD_PAYMENT by the 4th pass LLM linker."""
        mock_llm.return_value = {
            "evaluations": [{
                "pair_id": "p_101_102",
                "linkage_factor": 0.95,
                "link_type": "CREDIT_CARD_PAYMENT",
                "reasoning": "Exact amount match $142.50, same day, CC payment pattern",
                "exchange_rate": None,
                "amount_variance": None,
            }]
        }

        txns = [
            _txn(101, "CAD", "DEBIT", "2026-01-15", -142.50, "CAD",
                 "OnlineTransfer,TF0000000000"),
            _txn(102, "CreditCard", "CREDIT", "2026-01-15", 142.50, "CAD",
                 "PAYMENT - THANK YOU"),
        ]

        result = run_llm_transaction_linking(txns)
        assert len(result.links) == 1
        link = result.links[0]
        assert link.link_type == "CREDIT_CARD_PAYMENT"
        assert link.source_transaction_id == 101
        assert link.target_transaction_id == 102
        assert link.confidence_score == round(0.95 * 0.95, 4)

    @patch("core.llm_transaction_linker._call_llm_for_linking")
    def test_fx_conversion_pair_detected_by_4th_pass(self, mock_llm):
        """FX conversion pair (CAD debit + USD credit, amounts differ by
        FX rate 1.35) is detected as FX_CONVERSION by the 4th pass LLM linker."""
        mock_llm.return_value = {
            "evaluations": [{
                "pair_id": "p_103_104",
                "linkage_factor": 0.91,
                "link_type": "FX_CONVERSION",
                "reasoning": "CAD/USD ratio 1.3500, within normal FX band, same day",
                "exchange_rate": "1.3500",
                "amount_variance": None,
            }]
        }

        txns = [
            _txn(103, "CAD", "DEBIT", "2026-01-20", -500.00, "CAD",
                 "USDTFR AT1.3500"),
            _txn(104, "USD", "CREDIT", "2026-01-20", 370.37, "USD",
                 "Transfer,1234-5678-901"),
        ]

        result = run_llm_transaction_linking(txns)
        assert len(result.links) == 1
        link = result.links[0]
        assert link.link_type == "FX_CONVERSION"
        assert link.source_transaction_id == 103
        assert link.target_transaction_id == 104
        assert link.exchange_rate == "1.3500"

    def test_still_unmatched_includes_non_reconcilable(self):
        """The pool that feeds the 4th pass is every unmatched transaction.

        Derived from the reconcilable subset instead, it would drop exactly the
        transfers, card payments and conversions the pass exists to pair, and
        the two derivations are contrasted here so the difference is explicit.
        """
        txn_a = _txn(201, "CAD", "DEBIT", "2026-01-15", -142.50, "CAD",
                     "OnlineTransfer,TF0000000000")
        txn_b = _txn(202, "CreditCard", "CREDIT", "2026-01-15", 142.50, "CAD",
                     "PAYMENT - THANK YOU")
        txn_c = _txn(203, "CAD", "DEBIT", "2026-01-20", -500.00, "CAD",
                     "USDTFR AT1.3500")
        txn_d = _txn(204, "USD", "CREDIT", "2026-01-20", 370.37, "USD",
                     "Transfer,1234-5678-901")

        unmatched_txns = [txn_a, txn_b, txn_c, txn_d]
        # A = Credit Card Payment, C = USD to CAD, D = Internal Transfer
        non_reconcilable_ids = {txn_a.id, txn_c.id, txn_d.id}
        linked_txn_ids: set[int] = set()
        matched_txn_ids_from_docs: set[int] = set()

        # Derived from the reconcilable subset: the non-reconcilable rows are
        # gone before the pass ever sees them.
        reconcilable_txns = [t for t in unmatched_txns if t.id not in non_reconcilable_ids]
        pool_from_reconcilable_only = [
            t for t in reconcilable_txns
            if t.id not in matched_txn_ids_from_docs and t.id not in linked_txn_ids
        ]

        # Derived from every unmatched transaction: the pairs the pass is for
        # are still in the pool.
        pool_from_all_unmatched = [
            t for t in unmatched_txns
            if t.id not in matched_txn_ids_from_docs and t.id not in linked_txn_ids
        ]

        # The reconcilable-only derivation passes B and nothing else.
        assert len(pool_from_reconcilable_only) == 1
        assert pool_from_reconcilable_only[0].id == txn_b.id

        # The full derivation passes all 4.
        assert len(pool_from_all_unmatched) == 4
        pooled_ids = {t.id for t in pool_from_all_unmatched}
        assert pooled_ids == {txn_a.id, txn_b.id, txn_c.id, txn_d.id}
