"""Tests for ``core/match_explainer.py`` — the sentence a user reads.

Every figure, merchant and receipt in this module is invented — see
``tests/fixtures/README.md``.

``explain_match`` turns the reconciliation engine's numbers into English. Three
things are pinned here:

* each branch of the amount / date / vendor wording, and the confidence label
  and flags that go with it, including the boundaries between the bands;
* that the function **never raises**, whatever partial or malformed input it
  is handed — it is called while rendering a Review row, and an exception
  there costs the user the whole panel;
* that the one-line ``summary`` is a whole sentence, and that it carries the
  currency caveat, because ``server/api/reconciliation.py`` returns
  ``explanation.summary`` and discards every other field.
"""

from __future__ import annotations

from decimal import Decimal

from core.match_explainer import MatchExplanation, explain_match

# ─── The invented pair every "clean match" test starts from ─────────────────
#
# Example Corp Inc. pays a courier invoice by card; the receipt is in the
# folder, same figure, same day, and the courier's name is on the bank line.

COURIER_TOTAL = Decimal("104.55")
COURIER_VENDOR = "Meridian Courier"
COURIER_DESC = "MERIDIAN COURIER DELIVERY"
COURIER_DATE = "2026-05-12"


def _exact_match_kwargs(**overrides) -> dict:
    """Kwargs for a perfect same-currency exact match."""
    kwargs = dict(
        txn_amount=COURIER_TOTAL,
        txn_currency="CAD",
        txn_date=COURIER_DATE,
        txn_description=COURIER_DESC,
        doc_total=COURIER_TOTAL,
        doc_currency="CAD",
        doc_date=COURIER_DATE,
        doc_vendor=COURIER_VENDOR,
        confidence_score=0.95,
        amount_score=1.0,
        date_score=1.0,
        vendor_score=0.9,
        match_type="ONE_TO_ONE",
    )
    kwargs.update(overrides)
    return kwargs


# ═══════════════════════════════════════════════════════════════════
#  Exact same-currency amount match
# ═══════════════════════════════════════════════════════════════════


class TestExactMatch:
    def test_exact_amount_detail(self):
        result = explain_match(**_exact_match_kwargs())
        assert "$104.55 matches exactly" in result.amount_detail

    def test_exact_match_high_confidence(self):
        result = explain_match(**_exact_match_kwargs())
        assert result.confidence_label == "HIGH"

    def test_exact_match_no_flags(self):
        result = explain_match(**_exact_match_kwargs())
        assert result.flags == []


# ═══════════════════════════════════════════════════════════════════
#  Tolerance match (penny rounding)
# ═══════════════════════════════════════════════════════════════════


class TestToleranceMatch:
    def test_penny_rounding_detail(self):
        """A cent apart is a settlement rounding, and is named as one."""
        result = explain_match(**_exact_match_kwargs(
            doc_total=COURIER_TOTAL - Decimal("0.01"),
        ))
        assert "rounding difference" in result.amount_detail

    def test_three_cents_is_still_inside_the_penny_window(self):
        """$0.05 is the penny tolerance, so $0.03 is a rounding, not a gap.

        Both sides of that sentence are asserted: the wording says "rounding",
        and the exact difference is printed rather than rounded away.
        """
        result = explain_match(**_exact_match_kwargs(
            txn_amount=Decimal("300.00"), doc_total=Decimal("299.97"),
        ))
        assert "rounding difference" in result.amount_detail
        assert "$0.03" in result.amount_detail


# ═══════════════════════════════════════════════════════════════════
#  FX cross-currency
# ═══════════════════════════════════════════════════════════════════
#
# A contractor invoice in USD, settled out of the CAD chequing account. The two
# figures below are one conversion at 1.35 — a rate inside the band the engine
# holds — so the pair reconciles, and the wording has to say *converted*, never
# "exactly".

FX_INVOICE_USD = Decimal("1200.00")
FX_RATE = Decimal("1.35")
FX_BANK_CAD = -(FX_INVOICE_USD * FX_RATE)        # -1620.00


class TestFXCrossCurrency:
    def _fx_kwargs(self, **overrides) -> dict:
        return _exact_match_kwargs(
            txn_amount=FX_BANK_CAD, txn_currency="CAD",
            doc_total=FX_INVOICE_USD, doc_currency="USD",
            amount_score=0.78, confidence_score=0.70, **overrides,
        )

    def test_fx_amount_detail(self):
        result = explain_match(**self._fx_kwargs())
        assert "FX conversion" in result.amount_detail
        assert "implied rate" in result.amount_detail
        assert f"{FX_RATE:.4f}" in result.amount_detail

    def test_fx_flag_present(self):
        """The flag comes off the two currency codes, not off the rate.

        Deliberately asserted on the *unconverted* pair — the same magnitude in
        two currencies, which does not reconcile at any rate — because a user
        needs to be told a currency boundary was crossed whether or not the
        engine could get across it.
        """
        result = explain_match(**_exact_match_kwargs(
            doc_currency="USD", amount_score=0.0, confidence_score=0.70,
        ))
        assert "FX conversion applied" in result.flags

    def test_fx_confidence_medium(self):
        result = explain_match(**self._fx_kwargs())
        assert result.confidence_label == "MEDIUM"


# ═══════════════════════════════════════════════════════════════════
#  Split transaction
# ═══════════════════════════════════════════════════════════════════


class TestSplitTransaction:
    def test_split_flag_present(self):
        result = explain_match(**_exact_match_kwargs(
            match_type="MANY_TO_ONE", confidence_score=0.50,
        ))
        assert any(
            "Split transaction" in f and "MANY_TO_ONE" in f for f in result.flags
        )

    def test_split_confidence_low(self):
        result = explain_match(**_exact_match_kwargs(
            match_type="MANY_TO_ONE", confidence_score=0.50,
        ))
        assert result.confidence_label == "LOW"

    def test_one_to_many_split(self):
        result = explain_match(**_exact_match_kwargs(
            match_type="ONE_TO_MANY", confidence_score=0.50,
        ))
        assert any(
            "Split transaction" in f and "ONE_TO_MANY" in f for f in result.flags
        )


# ═══════════════════════════════════════════════════════════════════
#  No receipt date
# ═══════════════════════════════════════════════════════════════════


class TestNoReceiptDate:
    def test_no_date_on_receipt(self):
        result = explain_match(**_exact_match_kwargs(doc_date=None))
        assert "No date on receipt" in result.date_detail


# ═══════════════════════════════════════════════════════════════════
#  Large date gap > 14 days
# ═══════════════════════════════════════════════════════════════════


class TestLargeDateGap:
    #: 2026-06-15 against 2026-05-15 is 31 days — past the 14-day window in
    #: which a charge posting after a purchase is ordinary.
    LATE_TXN = "2026-06-15"
    EARLY_DOC = "2026-05-15"

    def test_unusual_delay_in_date_detail(self):
        result = explain_match(**_exact_match_kwargs(
            txn_date=self.LATE_TXN, doc_date=self.EARLY_DOC, date_score=0.3,
        ))
        assert "31 days apart" in result.date_detail
        assert "unusual delay" in result.date_detail

    def test_unusual_date_gap_flag(self):
        """The flag is driven by the sub-score, not by the day count."""
        result = explain_match(**_exact_match_kwargs(
            txn_date=self.LATE_TXN, doc_date=self.EARLY_DOC,
            date_score=0.3,   # < 0.5 triggers the flag
        ))
        assert "Unusual date gap" in result.flags


# ═══════════════════════════════════════════════════════════════════
#  Vendor not found
# ═══════════════════════════════════════════════════════════════════


class TestVendorNotFound:
    def test_vendor_not_found_detail(self):
        result = explain_match(**_exact_match_kwargs(vendor_score=0.1))
        assert "not found" in result.vendor_detail.lower()
        assert COURIER_VENDOR in result.vendor_detail

    def test_vendor_unconfirmed_flag(self):
        result = explain_match(**_exact_match_kwargs(vendor_score=0.1))
        assert "Vendor unconfirmed" in result.flags


# ═══════════════════════════════════════════════════════════════════
#  Payment intermediary vendor
# ═══════════════════════════════════════════════════════════════════


class TestPaymentIntermediary:
    def test_payment_intermediary_detail(self):
        """vendor_score >= 0.35 and < 0.8 → payment intermediary path."""
        result = explain_match(**_exact_match_kwargs(vendor_score=0.5))
        assert "payment intermediary" in result.vendor_detail.lower()


# ═══════════════════════════════════════════════════════════════════
#  HIGH confidence → summary starts with "Strong match:"
# ═══════════════════════════════════════════════════════════════════


class TestHighConfidence:
    def test_high_confidence_summary(self):
        result = explain_match(**_exact_match_kwargs(confidence_score=0.95))
        assert result.summary.startswith("Strong match:")


# ═══════════════════════════════════════════════════════════════════
#  MEDIUM confidence → "Probable match:" with "Review vendor"
# ═══════════════════════════════════════════════════════════════════


class TestMediumConfidence:
    def test_medium_confidence_summary(self):
        result = explain_match(**_exact_match_kwargs(confidence_score=0.70))
        assert result.summary.startswith("Probable match:")
        assert "Review vendor" in result.summary


# ═══════════════════════════════════════════════════════════════════
#  LOW confidence → summary starts with "Weak match:"
# ═══════════════════════════════════════════════════════════════════


class TestLowConfidence:
    def test_low_confidence_summary(self):
        result = explain_match(**_exact_match_kwargs(confidence_score=0.40))
        assert result.summary.startswith("Weak match:")


# ═══════════════════════════════════════════════════════════════════
#  Never raises on None/empty inputs
# ═══════════════════════════════════════════════════════════════════


class TestNeverRaises:
    def test_none_inputs(self):
        """All None/empty inputs should produce a valid MatchExplanation."""
        result = explain_match(
            txn_amount=None,
            txn_currency=None,
            txn_date=None,
            txn_description=None,
            doc_total=None,
            doc_currency=None,
            doc_date=None,
            doc_vendor=None,
            confidence_score=None,
            amount_score=None,
            date_score=None,
            vendor_score=None,
            match_type=None,
        )
        assert isinstance(result, MatchExplanation)
        assert isinstance(result.summary, str)
        assert isinstance(result.flags, list)

    def test_empty_strings(self):
        result = explain_match(
            txn_amount="",
            txn_currency="",
            txn_date="",
            txn_description="",
            doc_total="",
            doc_currency="",
            doc_date="",
            doc_vendor="",
            confidence_score="",
            amount_score="",
            date_score="",
            vendor_score="",
            match_type="",
        )
        assert isinstance(result, MatchExplanation)

    def test_zero_values(self):
        result = explain_match(
            txn_amount=Decimal("0"),
            txn_currency="CAD",
            txn_date="2026-05-01",
            txn_description="POS PURCHASE 0000",
            doc_total=Decimal("0"),
            doc_currency="CAD",
            doc_date="2026-05-01",
            doc_vendor="Cedarview Supplies Ltd",
            confidence_score=0.0,
            amount_score=0.0,
            date_score=0.0,
            vendor_score=0.0,
            match_type="ONE_TO_ONE",
        )
        assert isinstance(result, MatchExplanation)


# ═══════════════════════════════════════════════════════════════════
#  Return type is MatchExplanation dataclass
# ═══════════════════════════════════════════════════════════════════


class TestReturnType:
    def test_return_type_is_match_explanation(self):
        result = explain_match(**_exact_match_kwargs())
        assert isinstance(result, MatchExplanation)

    def test_all_fields_have_correct_types(self):
        result = explain_match(**_exact_match_kwargs())
        assert isinstance(result.summary, str)
        assert isinstance(result.amount_detail, str)
        assert isinstance(result.date_detail, str)
        assert isinstance(result.vendor_detail, str)
        assert isinstance(result.confidence_label, str)
        assert isinstance(result.flags, list)

    def test_confidence_label_is_valid_value(self):
        result = explain_match(**_exact_match_kwargs())
        assert result.confidence_label in ("HIGH", "MEDIUM", "LOW")


# ═══════════════════════════════════════════════════════════════════
#  Negative txn_amount absolute comparison
# ═══════════════════════════════════════════════════════════════════


class TestNegativeAmountAbsoluteComparison:
    def test_negative_txn_matches_positive_doc(self):
        """A bank debit is negative and a receipt total is positive.

        They are the same money, and the comparison is on magnitudes, so the
        pair reads as an exact match rather than a $209.10 discrepancy.
        """
        result = explain_match(**_exact_match_kwargs(txn_amount=-COURIER_TOTAL))
        assert "matches exactly" in result.amount_detail


# ═══════════════════════════════════════════════════════════════════
#  Same-day dates
# ═══════════════════════════════════════════════════════════════════


class TestSameDayDates:
    def test_same_day_in_date_detail(self):
        """When txn_date == doc_date, date_detail should contain 'Same day'."""
        result = explain_match(**_exact_match_kwargs())
        assert "Same day" in result.date_detail


# ═══════════════════════════════════════════════════════════════════
#  Boundary 0.85: HIGH vs MEDIUM confidence label
# ═══════════════════════════════════════════════════════════════════


class TestBoundaryConfidence085:
    def test_085_is_high(self):
        result = explain_match(**_exact_match_kwargs(confidence_score=0.85))
        assert result.confidence_label == "HIGH"

    def test_084_is_medium(self):
        result = explain_match(**_exact_match_kwargs(confidence_score=0.84))
        assert result.confidence_label == "MEDIUM"


# ═══════════════════════════════════════════════════════════════════
#  Boundary 0.60: MEDIUM vs LOW confidence label
# ═══════════════════════════════════════════════════════════════════


class TestBoundaryConfidence060:
    def test_060_is_medium(self):
        result = explain_match(**_exact_match_kwargs(confidence_score=0.60))
        assert result.confidence_label == "MEDIUM"

    def test_059_is_low(self):
        result = explain_match(**_exact_match_kwargs(confidence_score=0.59))
        assert result.confidence_label == "LOW"


# ═══════════════════════════════════════════════════════════════════
#  Boundary 0.35: vendor_score payment intermediary vs not found
# ═══════════════════════════════════════════════════════════════════


class TestBoundaryVendorScore035:
    def test_035_is_payment_intermediary(self):
        result = explain_match(**_exact_match_kwargs(vendor_score=0.35))
        assert "payment intermediary" in result.vendor_detail.lower()

    def test_034_is_not_found(self):
        result = explain_match(**_exact_match_kwargs(vendor_score=0.34))
        assert "not found" in result.vendor_detail.lower()


# ═══════════════════════════════════════════════════════════════════
#  The summary is a whole sentence, never a slice of one
# ═══════════════════════════════════════════════════════════════════
#
# A summary built by hard-slicing each detail string at 60 characters would
# come out of the panel like this:
#   "Strong match: $1,800.00 matches exactly, Transaction 2026-03-04 vs
#    receipt 2026-03-16 (12 days apart..., 'Jordan Avery Design' found in
#    bank description"
# — an ellipsis dropped mid-parenthesis, a quote left open and no closing
# bracket. The sentence has to be trimmed by clause instead, so whatever the
# reader gets is a whole sentence.


def _contractor_kwargs(**overrides) -> dict:
    """The pair the trimming tests are argued over.

    A US$1,800 invoice from an invented contractor, paid out of the USD
    account twelve days later — long enough in all three clauses that a
    character-slicing trimmer would cut one of them open.
    """
    kwargs = dict(
        txn_amount=Decimal("-1800.00"),
        txn_currency="USD",
        txn_date="2026-03-04",
        txn_description="Transfer to Jordan Avery",
        doc_total=Decimal("1800.00"),
        doc_currency="USD",
        doc_date="2026-03-16",
        doc_vendor="Jordan Avery Design",
        confidence_score=0.90,
        amount_score=1.0,
        date_score=0.5,
        vendor_score=0.8,
        match_type="ONE_TO_ONE",
    )
    kwargs.update(overrides)
    return kwargs


class TestSummaryIsWhole:
    def test_no_mid_sentence_ellipsis(self):
        summary = explain_match(**_contractor_kwargs()).summary
        assert "..." not in summary
        assert "…" not in summary

    def test_parentheses_are_balanced(self):
        summary = explain_match(**_contractor_kwargs()).summary
        assert summary.count("(") == summary.count(")")

    def test_quotes_are_balanced(self):
        summary = explain_match(**_contractor_kwargs()).summary
        assert summary.count("'") % 2 == 0

    def test_summary_ends_in_a_full_stop(self):
        summary = explain_match(**_contractor_kwargs()).summary
        assert summary.endswith(".")

    def test_all_three_signals_are_present(self):
        summary = explain_match(**_contractor_kwargs()).summary
        assert "matches exactly" in summary
        assert "12 days apart" in summary
        assert "Jordan Avery Design" in summary

    def test_a_very_long_bank_description_still_yields_a_whole_sentence(self):
        """The one clause a *document* can make arbitrarily long is trimmed.

        ``vendor_score`` of 0.1 forces the "not found in '<description>'"
        wording, which quotes the whole bank description. The trimmer may
        shorten it; it may not leave the sentence open.
        """
        summary = explain_match(**_contractor_kwargs(
            vendor_score=0.1,
            txn_description=(
                "POS PURCHASE 0000 A DELIBERATELY LONG INVENTED MERCHANT NAME "
                "(ANYTOWN MB) REF 000000000000000000 AUTH 0000"
            ),
        )).summary
        assert summary.count("(") == summary.count(")")
        assert summary.count("'") % 2 == 0
        assert summary.endswith(".")

    def test_detail_strings_do_not_nest_parentheses(self):
        detail = explain_match(**_contractor_kwargs()).date_detail
        assert "((" not in detail
        assert detail.count("(") == detail.count(")")


# ═══════════════════════════════════════════════════════════════════
#  The currency caveat has to reach the sentence a user reads
# ═══════════════════════════════════════════════════════════════════
#
# A subscription receipt — a screenshot printing a bare "$" whose CAD is
# *inferred* from a 12% tax line (5% GST + 7% PST) — matches its CAD
# credit-card line. Without the caveat below it would explain itself as
#
#   "Strong match: $5.60 matches exactly, 1 day apart, 'Bluepeak Software Inc'
#    found in the bank description. Settled deterministically ..."
#
# — with no mention of where that currency came from. A caveat that lives only
# in ``amount_detail`` never reaches anyone, because
# ``server/api/reconciliation.py`` returns ``explanation.summary`` and discards
# every other field. The manual-match path says "Receipt currency inferred as
# CAD from its Canadian tax line."; the automatic path has to say the same
# thing, in the same words, in the sentence that is actually shown.

SUBSCRIPTION_SUBTOTAL = Decimal("5.00")
SUBSCRIPTION_TAX = Decimal("0.60")
SUBSCRIPTION_TOTAL = SUBSCRIPTION_SUBTOTAL + SUBSCRIPTION_TAX      # 5.60


def _subscription_kwargs(**overrides) -> dict:
    """The pair above: a CAD card line and a bare-"$" subscription receipt."""
    kwargs = dict(
        txn_amount=-SUBSCRIPTION_TOTAL,
        txn_currency="CAD",
        txn_date="2026-03-11",
        txn_description="BLUEPEAK SOFT *Subscription ANYTOWN MB",
        doc_total=SUBSCRIPTION_TOTAL,
        doc_currency="CAD",
        doc_date="2026-03-10",
        doc_vendor="Bluepeak Software Inc",
        confidence_score=0.95,
        amount_score=1.0,
        date_score=0.97,
        vendor_score=0.8,
        match_type="ONE_TO_ONE",
        doc_currency_source="inferred",
    )
    kwargs.update(overrides)
    return kwargs


class TestInferredCurrencyIsNamedInTheSummary:
    def test_summary_carries_the_caveat(self):
        summary = explain_match(**_subscription_kwargs()).summary
        assert (
            "$5.60 matches exactly (receipt currency inferred as CAD from its "
            "Canadian tax line)"
        ) in summary

    def test_the_words_are_the_manual_paths_words(self):
        """Both surfaces describe one receipt the same way, or one of them lies.

        The manual picker's sentence comes from
        ``AmountComparison.currency_note``; the summary is that sentence in
        brackets. Deriving the expectation from the shared object, rather than
        typing the words out twice, is what keeps the two from drifting apart.
        """
        from core.amount_match import compare_amounts

        note = compare_amounts(
            -SUBSCRIPTION_TOTAL, "CAD", SUBSCRIPTION_TOTAL, "CAD",
            doc_currency_source="inferred",
        ).currency_note()
        assert note == "Receipt currency inferred as CAD from its Canadian tax line."

        bracketed = f"({note[0].lower()}{note[1:].rstrip('.')})"
        assert bracketed in explain_match(**_subscription_kwargs()).summary

    def test_the_caveat_survives_the_clause_trimmer(self):
        """The trimmer drops a whole parenthetical rather than break it.

        Under a single 80-character clause limit this caveat is what gets
        dropped, leaving a bare "$5.60 matches exactly". The amount clause has
        its own, longer limit so the caveat survives.
        """
        summary = explain_match(**_subscription_kwargs()).summary
        assert "…" not in summary
        assert "..." not in summary
        assert summary.count("(") == summary.count(")")
        assert summary.endswith(".")

    def test_a_long_bank_description_does_not_cost_the_caveat(self):
        summary = explain_match(**_subscription_kwargs(
            txn_description=(
                "POS PURCHASE 0000 BLUEPEAK SOFT *SUBSCRIPTION (ANYTOWN MB) REF "
                "000000000000000000 AUTH 0000"
            ),
            vendor_score=0.1,
        )).summary
        assert "receipt currency inferred as CAD" in summary
        assert summary.count("(") == summary.count(")")
        assert summary.count("'") % 2 == 0
        assert summary.endswith(".")

    def test_amount_detail_still_carries_it_too(self):
        detail = explain_match(**_subscription_kwargs()).amount_detail
        assert detail == (
            "$5.60 matches exactly (receipt currency inferred as CAD from its "
            "Canadian tax line)"
        )

    def test_the_flag_names_the_inference(self):
        result = explain_match(**_subscription_kwargs())
        assert result.flags == ["Receipt currency inferred from its tax line"]


class TestAssumedCurrencyIsNamedInTheSummary:
    def _kwargs(self, **overrides) -> dict:
        return _subscription_kwargs(
            doc_currency="", doc_currency_source="assumed", **overrides
        )

    def test_summary_says_the_receipt_never_stated_one(self):
        summary = explain_match(**self._kwargs()).summary
        assert (
            "$5.60 matches exactly (receipt states no currency; read as CAD)"
        ) in summary

    def test_summary_names_the_currency_it_was_read_in(self):
        """The phrase "read as USD" is the point: the engine's reading of the
        receipt, not something the receipt stated."""
        summary = explain_match(**self._kwargs(txn_currency="USD")).summary
        assert "read as USD" in summary

    def test_a_near_miss_amount_is_caveated_too(self):
        summary = explain_match(**self._kwargs(
            doc_total=SUBSCRIPTION_TOTAL - Decimal("0.03"), amount_score=0.9,
        )).summary
        assert "receipt states no currency; read as CAD" in summary

    def test_the_flag_says_nothing_was_stated(self):
        result = explain_match(**self._kwargs())
        assert result.flags == ["Receipt currency not stated"]


class TestStatedCurrencyNeedsNoCaveat:
    def test_the_sentence_gains_no_words(self):
        summary = explain_match(
            **_subscription_kwargs(doc_currency_source="stated")
        ).summary
        assert summary == (
            "Strong match: $5.60 matches exactly, 1 day apart, "
            "'Bluepeak Software Inc' found in the bank description."
        )

    def test_no_caveat_anywhere(self):
        result = explain_match(**_subscription_kwargs(doc_currency_source="stated"))
        for text in (result.summary, result.amount_detail):
            assert "inferred" not in text
            assert "read as" not in text
        assert result.flags == []

    def test_a_receipt_with_no_provenance_at_all_is_untouched(self):
        """Most callers pass no provenance at all; their sentence is unchanged."""
        kwargs = _subscription_kwargs()
        kwargs.pop("doc_currency_source")
        assert explain_match(**kwargs).summary == (
            "Strong match: $5.60 matches exactly, 1 day apart, "
            "'Bluepeak Software Inc' found in the bank description."
        )


# ═══════════════════════════════════════════════════════════════════
#  The deterministic-settle sentence says the same thing
# ═══════════════════════════════════════════════════════════════════
#
# ``core.reconciliation._unambiguous_candidate`` settles a pair whose currency
# was inferred from a Canadian tax line without asking an agent (proved in
# tests/test_amount_match_rule.py::test_an_inferred_currency_may_settle_one),
# and refuses only an *assumed* one. So the sentence ``_store_matches`` appends
# — "one candidate, the same amount in the same currency, ..." — is reachable
# for a currency the receipt never printed, and must not claim otherwise.


class _FakeDb:
    """Only the surface ``_store_matches`` touches for a clean settle."""

    def __init__(self) -> None:
        self.inserted: list = []
        self.txn_status: dict[int, str] = {}
        self.doc_status: dict[int, str] = {}

    def get_active_matches_for(self, transaction_ids, document_ids) -> list[dict]:
        return []

    def insert_match(self, match) -> int:
        return 9001

    def update_transaction_status(self, txn_id, status) -> None:
        self.txn_status[txn_id] = status

    def update_document_status(self, doc_id, status) -> None:
        self.doc_status[doc_id] = status


def _settled_explanation(**doc_overrides) -> str:
    """The stored sentence for the subscription pair, settled deterministically."""
    from datetime import date

    from models.document import Document, DocumentStatus
    from models.match import MatchStatus, MatchType, ReconciliationMatch
    from models.transaction import AccountType, Transaction, TransactionStatus
    from server.api.reconciliation import _store_matches

    txn = Transaction(
        id=901, account=AccountType.CREDIT_CARD, transaction_type="DEBIT",
        date_posted=date(2026, 3, 11), amount=-SUBSCRIPTION_TOTAL, currency="CAD",
        description="BLUEPEAK SOFT *Subscription ANYTOWN MB",
        source_file="statement_mar2026_card.csv", source_row=2,
        status=TransactionStatus.UNMATCHED,
    )
    fields = dict(
        id=2174, original_filename="20260310-bluepeak-subscription.png",
        file_hash="5555555555555555555555555555555e",
        vendor="Bluepeak Software Inc", document_date=date(2026, 3, 10),
        currency="USD",                  # the extractor's guess, no provenance
        subtotal=SUBSCRIPTION_SUBTOTAL, tax_other=SUBSCRIPTION_TAX,
        total=SUBSCRIPTION_TOTAL, status=DocumentStatus.EXTRACTED,
    )
    fields.update(doc_overrides)
    doc = Document(**fields)

    match = ReconciliationMatch(
        transaction_id=901, document_id=2174, confidence_score=0.95,
        amount_score=1.0, date_score=0.97, vendor_score=0.8,
        match_type=MatchType.ONE_TO_ONE, status=MatchStatus.PENDING_REVIEW,
        adjudication="deterministic",
    )
    db = _FakeDb()
    _store_matches(db, [match], {901: txn}, {2174: doc}, 0.90)
    assert match.explanation, "the pair should have been given an explanation"
    return match.explanation


class TestDeterministicSettleSentence:
    def test_an_inferred_currency_is_qualified(self):
        explanation = _settled_explanation()
        assert (
            "the same amount in the same currency (inferred from the receipt's "
            "Canadian tax line)"
        ) in explanation
        assert "the same amount in the same currency," not in explanation

    def test_the_summary_half_names_it_as_well(self):
        explanation = _settled_explanation()
        assert (
            "receipt currency inferred as CAD from its Canadian tax line"
        ) in explanation
        assert not explanation.startswith("Flagged for review")

    def test_a_stated_currency_keeps_the_plain_sentence(self):
        explanation = _settled_explanation(currency="CAD", currency_source="stated")
        assert (
            "Settled deterministically (adjudication: deterministic) — one "
            "candidate, the same amount in the same currency, dates within days, "
            "and the bank line names this vendor. No AI agent was asked."
        ) in explanation
        assert "inferred" not in explanation
