"""Tests for ``core/reconciliation.py`` — the scorers and the matcher.

Every figure, merchant and receipt in this module is invented — see
``tests/fixtures/README.md``. The company is Example Corp Inc.; its suppliers
are a telecom, a courier, a software vendor and a stationers, none of which
exist.

What is pinned here, bottom up:

* the four scorers a match is built out of — amount, date, vendor, and the
  weighted confidence over them — each at its boundaries;
* ``find_matches``, the whole pipeline, on the handful of shapes that decide
  whether a month reconciles: one obvious pair, a pair good enough to store
  without asking, a pair that must go to review instead, a pair that must not
  be stored at all, two recurring bills against two payments, and a document
  that may only be used once;
* the LLM prompt templates, which are ``str.format`` calls on text with
  literal braces in it — a stray brace there is a ``ValueError`` per document
  and a silently unmatched month;
* that a caller's broken ``progress_callback`` cannot take the run down.

The agents are deliberately not exercised: the fixture below leaves every one
of them on its deterministic fallback, so these tests describe the arithmetic
the engine does on its own and never open a socket.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from core.reconciliation import (
    compute_amount_score,
    compute_confidence,
    compute_date_score,
    compute_vendor_score,
    find_matches,
)
from models.document import Document, DocumentStatus
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, Transaction, TransactionStatus


@pytest.fixture(autouse=True)
def _no_hosted_llm(monkeypatch):
    """Keep every matcher agent on its deterministic fallback.

    ``conftest`` blanks the *local* endpoint, which is the engine's primary
    provider. The hosted fallbacks are switched off here too, because a
    contributor with ``OPENAI_API_KEY`` or ``GEMINI_API_KEY`` exported in their
    shell would otherwise have this file calling a hosted model once per
    document — a network round trip these tests must never make, and a
    non-deterministic answer in tests that assert exact scores.
    """
    import config.settings as settings

    monkeypatch.setattr(
        settings, "openai_config",
        replace(settings.openai_config, api_key="", enabled=False),
    )
    monkeypatch.setattr(
        settings, "gemini_config",
        replace(settings.gemini_config, api_key="", enabled=False),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_transaction(
    *,
    id: int = 1,
    amount: Decimal = Decimal("-104.55"),
    date_posted: date = date(2026, 5, 12),
    description: str = "ORCHID TELECOM PREAUTH PMT",
    currency: str = "CAD",
    account: AccountType | str = AccountType.CAD,
    transaction_type: str = "DEBIT",
) -> Transaction:
    return Transaction(
        id=id,
        account=account,
        transaction_type=transaction_type,
        date_posted=date_posted,
        amount=amount,
        currency=currency,
        description=description,
        source_file="statement_may2026_cad.csv",
        source_row=1,
        status=TransactionStatus.UNMATCHED,
    )


def _make_document(
    *,
    id: int = 1,
    total: Decimal = Decimal("104.55"),
    document_date: date = date(2026, 5, 12),
    vendor: str = "Orchid Telecom",
    currency: str = "CAD",
    filename: str = "20260512-orchid-telecom.pdf",
) -> Document:
    return Document(
        id=id,
        original_filename=filename,
        file_hash=f"{id:032d}",
        vendor=vendor,
        document_date=document_date,
        currency=currency,
        total=total,
        status=DocumentStatus.EXTRACTED,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Amount scoring
# ═════════════════════════════════════════════════════════════════════════════

class TestAmountScore:
    def test_amount_score_exact_match(self):
        """Same amount should produce a score of 1.0."""
        assert compute_amount_score(Decimal("481.25"), Decimal("481.25")) == 1.0

    def test_amount_score_no_match(self):
        """Different amounts should produce a score of 0.0."""
        assert compute_amount_score(Decimal("104.55"), Decimal("209.10")) == 0.0

    def test_amount_score_sign_normalized(self):
        """A bank debit is negative and a receipt total is positive.

        They are the same money, so the comparison is on magnitudes.
        """
        assert compute_amount_score(Decimal("-481.25"), Decimal("481.25")) == 1.0

    def test_amount_score_penny_difference(self):
        """Off by $0.01 should match with tolerance (FX/settlement rounding)."""
        assert compute_amount_score(Decimal("120.00"), Decimal("120.01")) == 0.9

    def test_amount_score_large_difference(self):
        """Off by more than $0.05 is not the same figure at all."""
        assert compute_amount_score(Decimal("120.00"), Decimal("120.10")) == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# Date scoring
# ═════════════════════════════════════════════════════════════════════════════

class TestDateScore:
    def test_date_score_same_day(self):
        """Same day should produce 1.0."""
        d = date(2026, 5, 12)
        assert compute_date_score(d, d) == 1.0

    def test_date_score_one_day(self):
        """1 day apart should score 0.9 — the charge posted overnight."""
        assert compute_date_score(date(2026, 5, 12), date(2026, 5, 13)) == 0.9

    def test_date_score_three_days(self):
        """3 days apart is the top of the same 0.9 bucket."""
        assert compute_date_score(date(2026, 5, 12), date(2026, 5, 15)) == 0.9

    def test_date_score_seven_days(self):
        """7 days apart should score 0.7."""
        assert compute_date_score(date(2026, 5, 12), date(2026, 5, 19)) == 0.7

    def test_date_score_far_apart(self):
        """Dates far apart (>45 days) should score very low but not zero.

        Not zero, because a strong amount and vendor still ought to be able to
        carry an invoice that sat in a drawer for a quarter.
        """
        score = compute_date_score(date(2026, 5, 12), date(2026, 8, 12))
        assert score > 0.0
        assert score < 0.1

    def test_date_score_none(self):
        """A receipt with no date at all gets a small baseline, not a zero."""
        assert compute_date_score(date(2026, 5, 12), None) == 0.1

    def test_date_score_symmetric(self):
        """A receipt dated after the payment scores as one dated before it."""
        assert compute_date_score(date(2026, 5, 15), date(2026, 5, 12)) == (
            compute_date_score(date(2026, 5, 12), date(2026, 5, 15))
        )


# ═════════════════════════════════════════════════════════════════════════════
# Vendor scoring
# ═════════════════════════════════════════════════════════════════════════════

class TestVendorScore:
    def test_vendor_score_keyword_match(self):
        """A word of the vendor's name inside the bank line is a strong signal."""
        score = compute_vendor_score("ORCHID TELECOM PREAUTH PMT", "Orchid Telecom")
        assert score >= 0.8

    def test_vendor_score_partial_match(self):
        """A rail prefix in front of the merchant does not hide the merchant."""
        score = compute_vendor_score("PAYPAL *BLUEPEAK", "Bluepeak Software Inc")
        assert score > 0.5

    def test_vendor_score_no_match(self):
        """Two names with nothing but incidental letters in common score low."""
        score = compute_vendor_score("EXAMPLE RETAILER MKTP CA", "Orchid Telecom")
        assert score < 0.3

    def test_vendor_score_case_insensitive(self):
        """Matching should be case-insensitive."""
        score = compute_vendor_score("bluepeak subscription", "BLUEPEAK SOFTWARE INC")
        assert score >= 0.8

    def test_vendor_score_empty_strings(self):
        """Nothing to compare is no evidence, not a coincidental match."""
        assert compute_vendor_score("", "Orchid Telecom") == 0.0
        assert compute_vendor_score("Orchid Telecom", "") == 0.0
        assert compute_vendor_score("", "") == 0.0


# ═════════════════════════════════════════════════════════════════════════════
# Confidence scoring
# ═════════════════════════════════════════════════════════════════════════════

class TestConfidence:
    def test_confidence_perfect_match(self):
        """All perfect scores should yield 1.0 confidence."""
        assert compute_confidence(1.0, 1.0, 1.0) == 1.0

    def test_confidence_no_match(self):
        """All zero scores should yield 0.0 confidence."""
        assert compute_confidence(0.0, 0.0, 0.0) == 0.0

    def test_confidence_weighted(self):
        """Verify default weights: 0.5*amount + 0.3*date + 0.2*vendor."""
        result = compute_confidence(1.0, 0.5, 0.0)
        expected = 0.5 * 1.0 + 0.3 * 0.5 + 0.2 * 0.0  # 0.65
        assert result == pytest.approx(expected)

    def test_confidence_custom_weights(self):
        """Custom weights should override defaults."""
        result = compute_confidence(
            1.0, 0.5, 0.0,
            amount_weight=0.7,
            date_weight=0.2,
            vendor_weight=0.1,
        )
        expected = 0.7 * 1.0 + 0.2 * 0.5 + 0.1 * 0.0  # 0.8
        assert result == pytest.approx(expected)

    def test_amount_is_the_heaviest_signal(self):
        """Half the confidence is the amount, and that is deliberate.

        Date and vendor together cannot carry a pair whose amounts never
        agreed; the amount is the only signal that is evidence on its own.
        """
        no_amount = compute_confidence(0.0, 1.0, 1.0)
        amount_only = compute_confidence(1.0, 0.0, 0.0)
        assert amount_only == pytest.approx(0.5)
        assert no_amount == pytest.approx(0.5)


# ═════════════════════════════════════════════════════════════════════════════
# find_matches
# ═════════════════════════════════════════════════════════════════════════════

class TestFindMatches:
    def test_find_matches_single_pair(self):
        """One transaction, one receipt, same figure and day → one match."""
        txn = _make_transaction(id=1)
        doc = _make_document(id=1)

        matches = find_matches([txn], [doc])

        assert len(matches) == 1
        m = matches[0]
        assert isinstance(m, ReconciliationMatch)
        assert m.transaction_id == 1
        assert m.document_id == 1
        assert m.amount_score == 1.0
        assert m.match_type == MatchType.ONE_TO_ONE

    def test_find_matches_auto_approve(self):
        """Confidence at or above the auto-approve threshold → AUTO_APPROVED.

        Same figure, two days apart, the vendor named on the bank line:
        0.5·1.0 + 0.3·0.9 + 0.2·0.8 = 0.93.
        """
        from config.settings import reconciliation_config

        txn = _make_transaction(
            id=1, amount=Decimal("-480.00"),
            description="BLUEPEAK SOFTWARE INC",
            date_posted=date(2026, 5, 22),
        )
        doc = _make_document(
            id=1, total=Decimal("480.00"), vendor="Bluepeak Software Inc",
            document_date=date(2026, 5, 20),
            filename="20260520-bluepeak-software.pdf",
        )

        matches = find_matches([txn], [doc])

        assert len(matches) == 1
        expected = compute_confidence(1.0, 0.9, 0.8)
        assert matches[0].confidence_score == pytest.approx(expected)
        assert matches[0].confidence_score >= reconciliation_config.auto_approve_threshold
        assert matches[0].status == MatchStatus.AUTO_APPROVED

    def test_find_matches_pending_review(self):
        """Between the review and auto-approve thresholds → PENDING_REVIEW.

        The figure agrees exactly, but the receipt is from a party the bank
        line does not name and is ten days off. That is a suggestion for a
        human, not a reconciliation.
        """
        from config.settings import reconciliation_config

        txn = _make_transaction(
            id=1, amount=Decimal("-81.45"),
            description="EXAMPLE RETAILER MKTP CA",
            date_posted=date(2026, 5, 20),
        )
        doc = _make_document(
            id=1, total=Decimal("81.45"), vendor="Northwind Trading Co.",
            document_date=date(2026, 5, 10),
            filename="20260510-northwind-trading.pdf",
        )

        matches = find_matches([txn], [doc])

        assert len(matches) == 1
        assert matches[0].confidence_score >= reconciliation_config.review_threshold
        assert matches[0].confidence_score < reconciliation_config.auto_approve_threshold
        assert matches[0].status == MatchStatus.PENDING_REVIEW

    def test_find_matches_no_match(self):
        """Nothing in common — amount, vendor or date — produces no match."""
        txn = _make_transaction(id=1)
        doc = _make_document(
            id=1, total=Decimal("9876.54"), vendor="Harbourline Logistics Ltd.",
            document_date=date(2026, 6, 28),
            filename="20260628-harbourline-logistics.pdf",
        )

        assert find_matches([txn], [doc]) == []

    def test_amount_mismatch_never_matches(self):
        """Different amounts never match, whatever the vendor and date say.

        Same supplier, same day, and a figure nearly three times the receipt's:
        the amount is the one signal that cannot be outvoted.
        """
        txn = _make_transaction(
            id=1, amount=Decimal("-2800.00"),
            description="CEDARVIEW SUPPLIES LTD",
            date_posted=date(2026, 6, 20),
        )
        doc = _make_document(
            id=1, total=Decimal("999.99"), vendor="Cedarview Supplies Ltd",
            document_date=date(2026, 6, 20),
            filename="20260620-cedarview-supplies.pdf",
        )

        assert find_matches([txn], [doc]) == []

    def test_recurring_bill_prefers_closer_date(self):
        """Two identical monthly bills, two identical payments, one each.

        The telecom bills $56.00 every month, and the pre-authorised debit
        clears a few days later. Both invoices and both payments are the same
        figure and the same vendor, so the *only* thing that can separate them
        is how far apart the dates are:

          payment 2026-02-05 → invoice 2026-02-03 (2 days, 0.9)
                             → invoice 2026-03-01 (24 days, 0.2)
          payment 2026-03-03 → invoice 2026-03-01 (2 days, 0.9)
                             → invoice 2026-02-03 (28 days, 0.2)

        Each payment therefore takes its own month's invoice, and no invoice is
        used twice.
        """
        txn_feb = _make_transaction(
            id=1, amount=Decimal("-56.00"), date_posted=date(2026, 2, 5),
        )
        txn_mar = _make_transaction(
            id=2, amount=Decimal("-56.00"), date_posted=date(2026, 3, 3),
        )
        doc_feb = _make_document(
            id=1, total=Decimal("56.00"), document_date=date(2026, 2, 3),
            filename="20260203-orchid-telecom.pdf",
        )
        doc_mar = _make_document(
            id=2, total=Decimal("56.00"), document_date=date(2026, 3, 1),
            filename="20260301-orchid-telecom.pdf",
        )

        matches = find_matches([txn_feb, txn_mar], [doc_feb, doc_mar])

        assert len(matches) == 2
        assert {(m.transaction_id, m.document_id) for m in matches} == {(1, 1), (2, 2)}
        for m in matches:
            assert m.date_score == 0.9

    def test_billing_cycle_matches(self):
        """An invoice dated the 19th SHOULD match a payment 14 days later.

        A recurring bill is issued well before it is collected; 14 days is the
        outer edge of the "normal processing delay" band, and a pair inside it
        with an exact amount and a named vendor still clears the floor.
        """
        from config.settings import reconciliation_config

        txn = _make_transaction(
            id=1, amount=Decimal("-56.00"), date_posted=date(2026, 3, 5),
        )
        doc = _make_document(
            id=1, total=Decimal("56.00"), document_date=date(2026, 2, 19),
            filename="20260219-orchid-telecom.pdf",
        )

        matches = find_matches([txn], [doc])
        assert len(matches) == 1
        assert matches[0].date_score == 0.5
        assert matches[0].confidence_score >= reconciliation_config.review_threshold

    def test_find_matches_best_match_wins(self):
        """Two receipts could fit one row; the one the bank line names wins."""
        txn = _make_transaction(
            id=1, amount=Decimal("-120.00"), description="MERIDIAN COURIER",
            date_posted=date(2026, 5, 15),
        )
        doc_good = _make_document(
            id=1, total=Decimal("120.00"), vendor="Meridian Courier",
            document_date=date(2026, 5, 15),
            filename="20260515-meridian-courier.pdf",
        )
        doc_bad = _make_document(
            id=2, total=Decimal("120.00"), vendor="Cedarview Supplies Ltd",
            document_date=date(2026, 5, 10),
            filename="20260510-cedarview-supplies.pdf",
        )

        matches = find_matches([txn], [doc_good, doc_bad])

        assert len(matches) == 1
        assert matches[0].document_id == 1

    def test_find_matches_no_double_matching(self):
        """One receipt proves one payment, however many payments look alike."""
        txn1 = _make_transaction(
            id=1, amount=Decimal("-120.00"), description="MERIDIAN COURIER",
            date_posted=date(2026, 5, 15),
        )
        txn2 = _make_transaction(
            id=2, amount=Decimal("-120.00"), description="MERIDIAN COURIER",
            date_posted=date(2026, 5, 15),
        )
        doc = _make_document(
            id=1, total=Decimal("120.00"), vendor="Meridian Courier",
            document_date=date(2026, 5, 15),
            filename="20260515-meridian-courier.pdf",
        )

        matches = find_matches([txn1, txn2], [doc])

        # Only one match should be created — the document can only be used once
        assert len(matches) == 1

    def test_find_matches_empty_inputs(self):
        """Empty transaction or document lists should return empty results."""
        assert find_matches([], []) == []
        assert find_matches([], [_make_document()]) == []
        assert find_matches([_make_transaction()], []) == []


# ═════════════════════════════════════════════════════════════════════════════
# Multi-pass runs
# ═════════════════════════════════════════════════════════════════════════════

class TestMultiPassRuns:
    """``find_matches_multi_pass`` — the entry point the API actually calls.

    It wraps ``find_matches`` with the audit log and the unmatched-item
    diagnosis, and the two things worth pinning are that the wrapper does not
    invent matches the matcher did not make, and that its diagnosis of what is
    left over is populated rather than empty.
    """

    @pytest.fixture(autouse=True)
    def _audit_log_in_tmp(self, tmp_path, monkeypatch):
        """Keep the run's audit log out of the working tree.

        ``find_matches_multi_pass`` flushes a JSON log per run; unlike
        ``find_matches`` it writes to disk, and the destination is a module
        constant read at the start of every run.
        """
        import core.reconciliation as recon

        monkeypatch.setattr(recon, "_RECON_LOG_DIR", tmp_path / "recon-logs")

    def test_three_identical_receipts_one_payment_gives_exactly_one_match(self):
        """Three copies of the same monthly bill, only one payment for it.

        All three receipts are an equally good amount match for the one
        payment, so all three compete for it. Exactly one may hold it; the
        other two must end the run unmatched rather than be spread across the
        unrelated payments that happen to sit on the same day.
        """
        from core.reconciliation import find_matches_multi_pass

        paid = _make_transaction(
            id=1, amount=Decimal("-1234.56"),
            description="CEDARVIEW SUPPLIES LTD", date_posted=date(2026, 9, 2),
        )
        other_1 = _make_transaction(
            id=2, amount=Decimal("-389.12"), description="MERIDIAN COURIER",
            date_posted=date(2026, 9, 2),
        )
        other_2 = _make_transaction(
            id=3, amount=Decimal("-402.93"), description="SAMPLE STREET CAFE",
            date_posted=date(2026, 9, 2),
        )
        docs = [
            _make_document(
                id=i, total=Decimal("1234.56"), vendor="Cedarview Supplies Ltd",
                document_date=doc_date,
                filename=f"{doc_date:%Y%m%d}-cedarview-supplies.pdf",
            )
            for i, doc_date in enumerate(
                (date(2026, 7, 31), date(2026, 8, 28), date(2026, 9, 15)), start=1,
            )
        ]

        result = find_matches_multi_pass([paid, other_1, other_2], docs)

        assert len(result.matches) == 1
        assert result.matches[0].transaction_id == 1
        assert result.matches[0].amount_score >= 0.9
        # The two receipts that lost, and the two unrelated payments, are all
        # accounted for with a reason rather than silently dropped.
        assert len(result.unmatched_documents) == 2
        assert len(result.unmatched_transactions) == 2
        assert all(r.reason for r in result.unmatched_documents)

    def test_a_payment_that_is_half_a_total_is_not_a_match(self):
        """Half of an invoice is not evidence of that invoice.

        $617.28 is exactly half of $1,234.56, the supplier is the same and the
        dates are days apart — the shape a tolerant "is this an instalment?"
        rule would accept. The engine has no such route: an amount that is 50%
        out is 50% out.
        """
        from core.reconciliation import find_matches_multi_pass

        txn = _make_transaction(
            id=1, amount=Decimal("-617.28"),
            description="CEDARVIEW SUPPLIES LTD", date_posted=date(2026, 9, 5),
        )
        doc = _make_document(
            id=1, total=Decimal("1234.56"), vendor="Cedarview Supplies Ltd",
            document_date=date(2026, 8, 28),
            filename="20260828-cedarview-supplies.pdf",
        )

        result = find_matches_multi_pass([txn], [doc])
        assert result.matches == []


# ═════════════════════════════════════════════════════════════════════════════
# Prompt formatting and pipeline robustness
# ═════════════════════════════════════════════════════════════════════════════

class TestPromptFormatting:
    """Every agent prompt template must survive ``str.format``.

    These templates contain literal JSON braces beside their ``{doc_info}``
    placeholders. A single unescaped brace raises
    ``ValueError: Single '}' encountered in format string`` on every call that
    reaches it — which is not a crash a user sees, because the agent's caller
    catches it and falls back, but a silent halving of the match rate.
    """

    DOC_INFO = (
        "Vendor: Cedarview Supplies Ltd | Date: 2026-05-12 | Total: CAD 104.55 "
        "| File: 20260512-cedarview-supplies.pdf"
    )
    TXN_LIST = "ID:610 | 2026-05-07 | CAD | -104.55 | CEDARVIEW SUPPLIES LTD"

    def test_date_agent_prompt_format_no_error(self):
        """The date prompt takes a third placeholder, and must still format."""
        from core.reconciliation import _DATE_AGENT_PROMPT

        result = _DATE_AGENT_PROMPT.format(
            doc_info=self.DOC_INFO, txn_list=self.TXN_LIST, max_date_days=45,
        )
        assert "45-day maximum date variance" in result
        assert isinstance(result, str)

    def test_final_agent_prompt_format_no_error(self):
        from core.reconciliation import _FINAL_AGENT_PROMPT

        result = _FINAL_AGENT_PROMPT.format(
            doc_info=self.DOC_INFO, txn_list=self.TXN_LIST,
        )
        assert "matched_transaction_id" in result
        assert isinstance(result, str)

    def test_amount_agent_prompt_format_no_error(self):
        from core.reconciliation import _AMOUNT_AGENT_PROMPT

        result = _AMOUNT_AGENT_PROMPT.format(
            doc_info=self.DOC_INFO, txn_list=self.TXN_LIST,
        )
        assert self.TXN_LIST in result
        assert isinstance(result, str)


class TestIsTransactionExpense:
    """``_is_transaction_expense`` decides which documents a row may match.

    The primary signal is ``transaction_type`` (``DEBIT`` = expense, ``CREDIT``
    = deposit/payment). The sign-based fallback only runs when
    ``transaction_type`` is absent.

    Why the type comes first: the LLM statement parser stores bank debits with a
    POSITIVE amount. Sign-only logic reads those as non-expenses, which makes
    ``check_sign_compatibility`` reject every candidate pair and the amount agent
    report "no amount match, skipped" for a whole upload.
    """

    def test_is_transaction_expense_credit_card(self):
        """Credit card DEBIT transactions (purchases) should be expenses."""
        from core.reconciliation import _is_transaction_expense

        txn = _make_transaction(
            amount=Decimal("41.25"), account=AccountType.CREDIT_CARD,
        )
        assert _is_transaction_expense(txn) is True

    def test_is_transaction_expense_credit_card_refund_is_not_expense(self):
        """Credit card CREDIT transactions (refunds/payments) are not expenses."""
        from core.reconciliation import _is_transaction_expense

        txn = _make_transaction(
            amount=Decimal("-41.25"),
            account=AccountType.CREDIT_CARD,
            transaction_type="CREDIT",
        )
        assert _is_transaction_expense(txn) is False

    def test_is_transaction_expense_bank_debit_negative_amount(self):
        """Bank DEBIT transactions with negative amount (legacy form) are expenses."""
        from core.reconciliation import _is_transaction_expense

        txn = _make_transaction(amount=Decimal("-104.55"), account=AccountType.CAD)
        assert _is_transaction_expense(txn) is True

    def test_is_transaction_expense_bank_debit_positive_amount(self):
        """A DEBIT with a POSITIVE amount is still an expense.

        This is the parser's canonical form: the account is a bank name string
        rather than one of the legacy enum members, and the sign says the
        opposite of the type.
        """
        from core.reconciliation import _is_transaction_expense

        txn = _make_transaction(
            amount=Decimal("104.55"),
            account="Example Bank",
            transaction_type="DEBIT",
        )
        assert _is_transaction_expense(txn) is True

    def test_is_transaction_expense_bank_deposit_not_expense(self):
        """Bank CREDIT transactions (deposits) should NOT be expenses."""
        from core.reconciliation import _is_transaction_expense

        txn = _make_transaction(
            amount=Decimal("9600.00"),
            account=AccountType.CAD,
            transaction_type="CREDIT",
        )
        assert _is_transaction_expense(txn) is False


class TestCallbackExceptionSafety:
    """A caller's broken progress callback must not take the run down."""

    def test_find_matches_survives_bad_callback(self):
        """``find_matches`` must not crash when ``progress_callback`` raises.

        The callback is how the reconcile page streams its log; a bug in it
        would otherwise cost the user the whole run rather than the progress
        display.
        """
        def bad_callback(info):
            raise RuntimeError("callback failure")

        txn = _make_transaction(id=1)
        doc = _make_document(id=1)

        try:
            results = find_matches([txn], [doc], progress_callback=bad_callback)
        except RuntimeError:
            pytest.fail(
                "progress_callback exception propagated to caller — "
                "the reporting wrapper is not exception-safe"
            )
        assert len(results) == 1


class TestAuditLogDirectory:
    """Where the per-run audit JSON lands when ``RECON_LOG_DIR`` is left empty.

    ``.env.example`` ships the key empty and documents that as "use
    ./logs/reconciliation". ``Path("")`` is the current directory, so reading
    the variable with a plain ``get`` default wrote a file of the run's
    transactions into the checkout root, where nothing in .gitignore covers it.
    """

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_value_means_the_default_directory(self, monkeypatch, value):
        import importlib
        from pathlib import Path

        import core.reconciliation as recon

        monkeypatch.setenv("RECON_LOG_DIR", value)
        try:
            reloaded = importlib.reload(recon)
            expected = Path(reloaded.__file__).resolve().parent.parent / "logs" / "reconciliation"
            assert reloaded._RECON_LOG_DIR == expected
        finally:
            monkeypatch.undo()
            importlib.reload(recon)

    def test_a_set_value_is_used(self, monkeypatch, tmp_path):
        import importlib

        import core.reconciliation as recon

        monkeypatch.setenv("RECON_LOG_DIR", str(tmp_path))
        try:
            assert importlib.reload(recon)._RECON_LOG_DIR == tmp_path
        finally:
            monkeypatch.undo()
            importlib.reload(recon)
