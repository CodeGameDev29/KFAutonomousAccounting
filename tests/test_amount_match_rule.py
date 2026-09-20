"""One amount-comparison rule, used by every scorer that has an opinion.

Every figure in this module is invented — see ``tests/fixtures/README.md``.

A bank line reading ``WISE TRANSFER -$2,400.00 CAD`` and a receipt printing
``US$2,400.00`` carry the same *number* in two currencies. A magnitude-only
comparison calls such a pair identical, so without one shared rule the auto
matcher, the manual-match dialog and the Review panel can each answer
differently about it — and at most one of those answers can be true.

These tests pin the rule (:func:`core.amount_match.compare_amounts`) and then
assert that the three surfaces agree with it: the auto matcher's FX scorer, the
manual picker's ``score_manual_match``, and the explanation text the Review
panel shows.

The second half pins the opposite direction. A receipt that never printed a
currency at all must not be *guessed* into one and then failed for disagreeing
with the guess: the invented subscription receipt below prints a bare ``$`` and
a 12% tax line (5% GST + 7% provincial sales tax), which is Canadian on its own
arithmetic, and it has to reconcile against the CAD card charge it is proof of.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.amount_match import (
    ASSUMED_SAME_CURRENCY,
    EXACT,
    FX_BAND,
    FX_BAND_SCORE,
    FX_CAD_USD_MAX,
    FX_CAD_USD_MIN,
    MISMATCH,
    NO_FX_BAND,
    ROUNDING,
    UNKNOWN,
    compare_amounts,
    compare_amounts_for_document,
)
from core.document_currency import CANADIAN_TAX_RATES_PCT
from core.match_explainer import explain_match
from core.reconciliation import compute_amount_score_fx, score_manual_match

CAD = "CAD"
USD = "USD"

# ── The invented cross-currency pair ──────────────────────────────────────
#
# Example Corp Inc. pays an invented contractor, Jordan Avery, through Wise.
# The receipt is stated in USD; the chequing account is in CAD. The two figures
# below are the same *number* in two currencies, which is the whole trap: a
# magnitude comparison calls them identical and they are nothing of the kind.
CONTRACTOR_INVOICE_USD = Decimal("2400.00")
CONTRACTOR_BANK_CAD = Decimal("-2400.00")

# The rate the settlement is stated at, and the CAD figure it produces.
# Derived, not quoted: change the rate and every expectation below follows.
SETTLEMENT_RATE = Decimal("1.35")
SETTLED_BANK_CAD = -(CONTRACTOR_INVOICE_USD * SETTLEMENT_RATE)   # -3240.00

# Same-currency figures, for the boundaries that have nothing to do with FX.
SUPPLIES_RECEIPT = Decimal("81.45")
OTHER_RECEIPT = Decimal("104.55")
COURIER_RECEIPT = Decimal("120.00")

# A bank fee deducted from an incoming wire — the window that must stay open.
WIRE_FEE = Decimal("15.00")


def test_the_settlement_rate_is_inside_the_band_the_engine_holds():
    """The scenario above is only meaningful if 1.35 is a plausible rate.

    Asserted rather than assumed, so a change to ``FX_CAD_USD_MIN``/``MAX``
    fails here — where the reason is written down — instead of failing six
    expectations further down that look like arithmetic errors.
    """
    assert FX_CAD_USD_MIN <= SETTLEMENT_RATE <= FX_CAD_USD_MAX
    # And the trap pair's implied rate is 1.0, which is outside it.
    assert not (FX_CAD_USD_MIN <= Decimal("1") <= FX_CAD_USD_MAX)


# ── The rule itself ────────────────────────────────────────────────────────

class TestSameCurrency:
    def test_exact_match(self):
        c = compare_amounts(-SUPPLIES_RECEIPT, CAD, SUPPLIES_RECEIPT, CAD)
        assert c.verdict == EXACT
        assert c.score == 1.0
        assert c.is_exact
        assert "Exact amount match" in c.phrase()

    def test_penny_rounding_is_not_exact(self):
        """Three cents apart is a settlement rounding, not the same figure."""
        c = compare_amounts(
            COURIER_RECEIPT, CAD, COURIER_RECEIPT + Decimal("0.03"), CAD,
        )
        assert c.verdict == ROUNDING
        assert c.score == 0.9
        assert not c.is_exact

    def test_outside_tolerance_is_mismatch(self):
        c = compare_amounts(-SUPPLIES_RECEIPT, CAD, OTHER_RECEIPT, CAD)
        assert c.verdict == MISMATCH
        assert c.score == 0.0
        assert "mismatch" in c.phrase().lower()

    def test_caller_tolerance_widens_only_same_currency(self):
        """A relaxed pass may pass a dollar; it may not pass a currency."""
        c = compare_amounts(
            COURIER_RECEIPT, CAD, COURIER_RECEIPT + Decimal("1.00"), CAD,
            tolerance=Decimal("2.00"),
        )
        assert c.score == 0.8
        # The same $1 gap across currencies is not a tolerance question at all:
        # 120 CAD against 121 USD is an implied rate of 0.99, which no amount of
        # caller tolerance makes into a conversion.
        c2 = compare_amounts(
            COURIER_RECEIPT, CAD, COURIER_RECEIPT + Decimal("1.00"), USD,
            tolerance=Decimal("2.00"),
        )
        assert c2.verdict == MISMATCH
        assert c2.score == 0.0

    def test_missing_document_total(self):
        c = compare_amounts(COURIER_RECEIPT, CAD, None, CAD)
        assert c.verdict == UNKNOWN
        assert c.score == 0.0


class TestCrossCurrency:
    def test_inside_band_is_an_fx_match_never_exact(self):
        c = compare_amounts(
            SETTLED_BANK_CAD, CAD, CONTRACTOR_INVOICE_USD, USD,
        )
        assert c.verdict == FX_BAND
        assert c.score == FX_BAND_SCORE
        assert not c.is_exact
        assert c.implied_rate == SETTLEMENT_RATE
        assert "exact" not in c.phrase().lower()

    def test_band_edges_are_inclusive(self):
        low = compare_amounts(
            Decimal("100") * FX_CAD_USD_MIN, CAD, Decimal("100"), USD,
        )
        high = compare_amounts(
            Decimal("100") * FX_CAD_USD_MAX, CAD, Decimal("100"), USD,
        )
        assert low.verdict == FX_BAND
        assert high.verdict == FX_BAND

    def test_the_trap_pair_does_not_reconcile(self):
        """CAD 2,400 vs USD 2,400 — implied rate 1.0, outside every band.

        The receipt states USD: it prints "US$2,400.00". A stored currency code
        with no provenance recorded beside it is read as stated too, which is
        what this call exercises.
        """
        c = compare_amounts(
            CONTRACTOR_BANK_CAD, CAD, CONTRACTOR_INVOICE_USD, USD,
        )
        assert c.verdict == MISMATCH
        assert c.score == 0.0
        assert not c.is_exact
        assert "do not reconcile" in c.phrase()

    def test_the_trap_pair_does_not_reconcile_when_stated_explicitly(self):
        """Same pair, provenance spelled out — the rule must not loosen here."""
        c = compare_amounts(
            CONTRACTOR_BANK_CAD, CAD, CONTRACTOR_INVOICE_USD, USD,
            doc_currency_source="stated",
        )
        assert c.verdict == MISMATCH
        assert c.score == 0.0
        assert "do not reconcile" in c.phrase()

    def test_rate_above_band_is_a_mismatch(self):
        """Double the invoice is an implied rate of 2.0 — a coincidence, not FX."""
        c = compare_amounts(
            -(CONTRACTOR_INVOICE_USD * 2), CAD, CONTRACTOR_INVOICE_USD, USD,
        )
        assert c.verdict == MISMATCH

    def test_usd_transaction_cad_receipt_uses_the_same_band(self):
        c = compare_amounts(
            -CONTRACTOR_INVOICE_USD, USD,
            CONTRACTOR_INVOICE_USD * SETTLEMENT_RATE, CAD,
        )
        assert c.verdict == FX_BAND
        assert c.implied_rate == SETTLEMENT_RATE

    def test_currency_pair_with_no_band_is_unknown_not_matched(self):
        c = compare_amounts(-COURIER_RECEIPT, CAD, COURIER_RECEIPT, "EUR")
        assert c.verdict == NO_FX_BAND
        assert c.score == 0.0


# ── The three surfaces agree with it ──────────────────────────────────────

class TestAllThreeScorersAgree:
    def _manual_amount_score(self, txn_amt, txn_cur, doc_amt, doc_cur) -> float:
        _, amount_score, _, _ = score_manual_match(
            txn_amt, txn_cur, date(2026, 3, 20), "WISE TRANSFER",
            doc_amt, doc_cur, date(2026, 3, 20), "Jordan Avery Design",
        )
        return amount_score

    def test_trap_pair_rejected_by_matcher_and_manual_picker_alike(self):
        args = (CONTRACTOR_BANK_CAD, CAD, CONTRACTOR_INVOICE_USD, USD)
        assert compute_amount_score_fx(args[0], args[2], args[1], args[3]) == 0.0
        assert self._manual_amount_score(*args) == 0.0

    def test_trap_pair_is_never_described_as_exact(self):
        """The amount sub-score handed in is deliberately the wrong one.

        The explainer is given ``amount_score=1.0`` on purpose: it must reach
        its own verdict from the two currencies rather than narrating whatever
        number the caller carried in.
        """
        explanation = explain_match(
            txn_amount=CONTRACTOR_BANK_CAD, txn_currency=CAD,
            txn_date="2026-03-20", txn_description="WISE TRANSFER",
            doc_total=CONTRACTOR_INVOICE_USD, doc_currency=USD,
            doc_date="2026-04-01", doc_vendor="Jordan Avery Design",
            confidence_score=0.77, amount_score=1.0, date_score=0.73,
            vendor_score=0.24, match_type="ONE_TO_ONE",
        )
        assert "matches exactly" not in explanation.amount_detail
        assert "Exact amount match" not in explanation.summary
        assert "no plausible FX conversion" in explanation.amount_detail
        assert "FX conversion applied" in explanation.flags

    def test_cross_currency_in_band_scores_the_same_everywhere(self):
        args = (SETTLED_BANK_CAD, CAD, CONTRACTOR_INVOICE_USD, USD)
        assert compute_amount_score_fx(args[0], args[2], args[1], args[3]) == FX_BAND_SCORE
        assert self._manual_amount_score(*args) == FX_BAND_SCORE

    def test_same_currency_exact_scores_one_everywhere(self):
        args = (-SUPPLIES_RECEIPT, CAD, SUPPLIES_RECEIPT, CAD)
        assert compute_amount_score_fx(args[0], args[2], args[1], args[3]) == 1.0
        assert self._manual_amount_score(*args) == 1.0

    def test_manual_picker_still_ranks_same_currency_near_misses(self):
        """A graded score is kept for ranking, but only within one currency."""
        close = self._manual_amount_score(
            -COURIER_RECEIPT, CAD, COURIER_RECEIPT - Decimal("6.00"), CAD,
        )
        far = self._manual_amount_score(
            -COURIER_RECEIPT, CAD, Decimal("6.00"), CAD,
        )
        assert 0.0 < far < close < 1.0


class TestEveryEngineScorerIsCurrencyAware:
    """The same rule in the two places that decide what a match *scores*."""

    def _txn(self, amount: Decimal, currency: str):
        from models.transaction import AccountType, Transaction, TransactionStatus
        return Transaction(
            id=11, account=AccountType.CAD, transaction_type="DEBIT",
            date_posted=date(2026, 3, 20), amount=amount,
            currency=currency, description="WISE TRANSFER",
            source_file="statement_mar2026_cad.csv", source_row=2,
            status=TransactionStatus.UNMATCHED,
        )

    def _doc(self, total: Decimal, currency: str):
        from models.document import Document, DocumentStatus
        return Document(
            id=21, original_filename="20260401-jordan-avery.pdf",
            file_hash="4444444444444444444444444444444d",
            vendor="Jordan Avery Design", document_date=date(2026, 4, 1),
            currency=currency, total=total,
            status=DocumentStatus.EXTRACTED,
        )

    def test_stored_amount_score_is_not_100_for_the_trap_pair(self):
        """The stored sub-score the Review panel renders under "Amount (50%)"."""
        from core.reconciliation import _compute_deterministic_amount_score

        assert _compute_deterministic_amount_score(
            self._doc(CONTRACTOR_INVOICE_USD, USD),
            self._txn(CONTRACTOR_BANK_CAD, CAD),
        ) == 0.0

    def test_stored_amount_score_honours_the_fx_band(self):
        from core.reconciliation import _compute_deterministic_amount_score

        assert _compute_deterministic_amount_score(
            self._doc(CONTRACTOR_INVOICE_USD, USD),
            self._txn(SETTLED_BANK_CAD, CAD),
        ) == FX_BAND_SCORE

    def test_same_currency_keeps_the_wire_fee_window(self):
        """Banks deduct a handling fee from an incoming wire; that stays a match."""
        from core.reconciliation import _compute_deterministic_amount_score

        assert _compute_deterministic_amount_score(
            self._doc(CONTRACTOR_INVOICE_USD, CAD),
            self._txn(-(CONTRACTOR_INVOICE_USD - WIRE_FEE), CAD),
        ) > 0.0

    def test_unmatched_diagnosis_does_not_invent_a_cross_currency_candidate(self):
        """A pair the matcher itself rejects is not offered as a best candidate.

        Reporting "Best candidate ... (score: 70%)" for a pairing the scorer
        gives 0 only invites a reviewer to accept it: no candidate at all is
        better than a wrong one."""
        from core.reconciliation import _diagnose_unmatched_doc

        reason = _diagnose_unmatched_doc(
            self._doc(CONTRACTOR_INVOICE_USD, USD),
            [self._txn(CONTRACTOR_BANK_CAD, CAD)],
        )
        assert reason.reason == "no_matching_amount"
        assert reason.best_candidate_score is None

    def test_unmatched_diagnosis_still_reports_a_real_fx_candidate(self):
        from core.reconciliation import _diagnose_unmatched_doc

        reason = _diagnose_unmatched_doc(
            self._doc(CONTRACTOR_INVOICE_USD, USD),
            [self._txn(SETTLED_BANK_CAD, CAD)],
        )
        assert reason.reason == "below_threshold"
        assert reason.best_candidate_score > 0


# ── A currency the receipt never stated ───────────────────────────────────
#
# The invented pair: a credit-card line `BLUEPEAK SOFT *Subscription ANYTOWN MB`
# for -$5.60 CAD (2026-03-11) against its own receipt — Bluepeak Software Inc,
# 2026-03-10, subtotal 5.00, other tax 0.60, total 5.60, and a bare "$" on the
# page. A stored currency of USD taken from a US mailing address in the footer,
# rather than from anything the page states, makes a currency-aware comparison
# score the pair Amount 0 — "Cross-currency amounts do not reconcile at any
# plausible rate" — unless the unstated currency is inferred from the tax line.

SUBSCRIPTION_SUBTOTAL = Decimal("5.00")
SUBSCRIPTION_TAX = Decimal("0.60")
SUBSCRIPTION_TOTAL = SUBSCRIPTION_SUBTOTAL + SUBSCRIPTION_TAX    # 5.60
SUBSCRIPTION_BANK_CAD = -SUBSCRIPTION_TOTAL

SUBSCRIPTION_DOC = {
    "vendor": "Bluepeak Software Inc",
    "currency": "USD",            # stored with no provenance recorded
    "subtotal": str(SUBSCRIPTION_SUBTOTAL),
    "tax_other": str(SUBSCRIPTION_TAX),
    "total": str(SUBSCRIPTION_TOTAL),
}

SUBSCRIPTION_DESCRIPTION = "BLUEPEAK SOFT *Subscription ANYTOWN MB"
SUBSCRIPTION_TXN_DATE = date(2026, 3, 11)
SUBSCRIPTION_DOC_DATE = date(2026, 3, 10)


def test_the_subscription_receipts_tax_line_is_arithmetically_canadian():
    """0.60 on 5.00 is 12% — 5% GST + 7% provincial sales tax — and nowhere else charges it.

    The inference below rests entirely on this ratio, so the ratio is asserted
    here rather than left as a number the reader has to trust.
    """
    rate_pct = SUBSCRIPTION_TAX / SUBSCRIPTION_SUBTOTAL * Decimal(100)
    assert rate_pct == Decimal("12")
    assert rate_pct in CANADIAN_TAX_RATES_PCT


class TestCurrencyTheReceiptNeverStated:
    def test_no_currency_at_all_is_read_in_the_transactions(self):
        c = compare_amounts(
            SUBSCRIPTION_BANK_CAD, CAD, SUBSCRIPTION_TOTAL, None,
        )
        assert c.verdict == ASSUMED_SAME_CURRENCY
        assert c.score == 1.0
        assert c.same_currency
        assert not c.is_exact          # it is not an unqualified exact match
        assert c.magnitude_verdict == EXACT
        phrase = c.phrase()
        assert "does not state a currency" in phrase
        assert "read as CAD" in phrase
        assert "Exact amount match" in phrase

    def test_a_bare_dollar_sign_is_read_the_same_way(self):
        c = compare_amounts(
            SUBSCRIPTION_BANK_CAD, CAD, SUBSCRIPTION_TOTAL, "$",
        )
        assert c.verdict == ASSUMED_SAME_CURRENCY
        assert c.score == 1.0

    def test_an_assumed_currency_gets_no_fx_rescue(self):
        """Unknown currency scores same-currency only — never through a band.

        $100 CAD against a receipt for 137 in nobody-knows-what is a 1.37
        implied rate, which is inside the CAD/USD band. It is not a match:
        nothing says the receipt is in USD, and deciding that it must be
        because the numbers then work is exactly the guess this rule removes.
        """
        plausible_rate = Decimal("1.37")
        assert FX_CAD_USD_MIN <= plausible_rate <= FX_CAD_USD_MAX

        c = compare_amounts(
            Decimal("-100.00"), CAD, Decimal("100.00") * plausible_rate, None,
        )
        assert c.verdict == MISMATCH
        assert c.score == 0.0
        assert c.implied_rate is None
        assert "does not state a currency" in c.phrase()

    def test_the_subscription_receipt_reconciles_through_its_tax_line(self):
        c = compare_amounts_for_document(
            SUBSCRIPTION_BANK_CAD, CAD, SUBSCRIPTION_TOTAL, SUBSCRIPTION_DOC,
        )
        assert c.verdict == EXACT
        assert c.score == 1.0
        assert c.doc_currency_source == "inferred"
        assert "inferred as CAD" in c.phrase()

    def test_a_stated_usd_receipt_with_a_canadian_looking_tax_line_stays_usd(self):
        doc = dict(SUBSCRIPTION_DOC, currency_source="stated")
        c = compare_amounts_for_document(
            SUBSCRIPTION_BANK_CAD, CAD, SUBSCRIPTION_TOTAL, doc,
        )
        assert c.verdict == MISMATCH
        assert c.score == 0.0


class TestTheSubscriptionPairEndToEnd:
    """What the Review panel shows for the subscription pair above."""

    def _score(self):
        return score_manual_match(
            SUBSCRIPTION_BANK_CAD, CAD, SUBSCRIPTION_TXN_DATE,
            SUBSCRIPTION_DESCRIPTION,
            SUBSCRIPTION_TOTAL, "USD", SUBSCRIPTION_DOC_DATE,
            "Bluepeak Software Inc",
            doc_fields=SUBSCRIPTION_DOC,
        )

    def test_amount_is_a_hundred_not_zero(self):
        _, amount_score, _, _ = self._score()
        assert amount_score == 1.0

    def test_vendor_is_the_engines_own_score(self):
        """compute_vendor_score sees "bluepeak" in the bank line: 0.80.

        The manual picker has to share that scorer rather than compare the two
        strings directly: a bare ``token_sort_ratio`` sees two differently
        ordered strings and scores them far lower.
        """
        _, _, _, vendor_score = self._score()
        assert vendor_score >= 0.8

    def test_the_pair_is_now_a_confident_match(self):
        from core.match_explainer import _confidence_label

        confidence, amount_score, date_score, vendor_score = self._score()
        # One day apart out of the 45-day window the manual picker allows.
        assert date_score == round(1.0 - 1 / 45, 4)
        assert date_score > 0.95
        # PASS_STRICT weights: amount 50%, date 30%, vendor 20%.
        assert confidence == round(
            0.5 * amount_score + 0.3 * date_score + 0.2 * vendor_score, 4,
        )
        assert _confidence_label(confidence) == "HIGH"

    def test_the_stored_sentence_says_how_the_currency_was_arrived_at(self):
        from server.api.reconciliation import _manual_match_explanation

        confidence, amt, dt, vnd = self._score()
        sentence = _manual_match_explanation(
            SUBSCRIPTION_BANK_CAD, CAD, SUBSCRIPTION_TXN_DATE,
            SUBSCRIPTION_TOTAL, "USD", SUBSCRIPTION_DOC_DATE,
            "Bluepeak Software Inc", amt, dt, vnd,
            doc_fields=SUBSCRIPTION_DOC,
        )
        assert "inferred as CAD" in sentence
        assert "Exact amount match" in sentence
        # One day is not "1 days".
        assert "Date 1 day apart" in sentence
        assert "1 days" not in sentence
        assert "Vendor match (Bluepeak Software Inc)" in sentence


class TestManualScorerAgreesWithTheEngineOnVendors:
    def test_the_bank_line_that_names_the_vendor_scores_the_same_in_both(self):
        from core.reconciliation import compute_vendor_score

        vendor = "Bluepeak Software Inc"
        _, _, _, manual = score_manual_match(
            SUBSCRIPTION_BANK_CAD, CAD, SUBSCRIPTION_TXN_DATE,
            SUBSCRIPTION_DESCRIPTION,
            SUBSCRIPTION_TOTAL, CAD, SUBSCRIPTION_DOC_DATE, vendor,
        )
        assert manual == round(
            compute_vendor_score(SUBSCRIPTION_DESCRIPTION, vendor), 4,
        )


class TestTheDeterministicScorerReadsTheSameCurrency:
    def _txn(self, amount: Decimal, currency: str):
        from models.transaction import AccountType, Transaction, TransactionStatus
        return Transaction(
            id=12, account=AccountType.CREDIT_CARD, transaction_type="DEBIT",
            date_posted=SUBSCRIPTION_TXN_DATE, amount=amount,
            currency=currency, description=SUBSCRIPTION_DESCRIPTION,
            source_file="statement_mar2026_card.csv", source_row=2,
            status=TransactionStatus.UNMATCHED,
        )

    def _doc(self, **overrides):
        from models.document import Document, DocumentStatus
        fields = dict(
            id=22, original_filename="20260310-bluepeak-subscription.png",
            file_hash="5555555555555555555555555555555e",
            vendor="Bluepeak Software Inc", document_date=SUBSCRIPTION_DOC_DATE,
            currency="USD", subtotal=SUBSCRIPTION_SUBTOTAL,
            tax_other=SUBSCRIPTION_TAX, total=SUBSCRIPTION_TOTAL,
            status=DocumentStatus.EXTRACTED,
        )
        fields.update(overrides)
        return Document(**fields)

    def test_stored_amount_score_is_one_for_the_subscription_pair(self):
        from core.reconciliation import _compute_deterministic_amount_score

        assert _compute_deterministic_amount_score(
            self._doc(), self._txn(SUBSCRIPTION_BANK_CAD, CAD),
        ) == 1.0

    def test_the_deterministic_shortlist_finds_it(self):
        from core.reconciliation import _deterministic_amount_shortlist

        txn = self._txn(SUBSCRIPTION_BANK_CAD, CAD)
        assert _deterministic_amount_shortlist(self._doc(), [txn]) == [txn.id]

    def test_a_receipt_with_no_currency_at_all_is_still_shortlisted(self):
        from core.reconciliation import _deterministic_amount_shortlist

        txn = self._txn(SUBSCRIPTION_BANK_CAD, CAD)
        doc = self._doc(currency=None, subtotal=None, tax_other=None)
        assert _deterministic_amount_shortlist(doc, [txn]) == [txn.id]

    def test_an_assumed_currency_never_settles_a_match_without_the_agents(self):
        """It may score the pair; it may not close it (UNAMBIGUOUS gate)."""
        from core.reconciliation import _unambiguous_candidate

        txn = self._txn(SUBSCRIPTION_BANK_CAD, CAD)
        doc = self._doc(currency=None, subtotal=None, tax_other=None)
        assert _unambiguous_candidate(doc, [txn.id], {txn.id: txn}, 45) is None

    def test_an_inferred_currency_may_settle_one(self):
        from core.reconciliation import _unambiguous_candidate

        txn = self._txn(SUBSCRIPTION_BANK_CAD, CAD)
        assert _unambiguous_candidate(
            self._doc(), [txn.id], {txn.id: txn}, 45,
        ) is txn
