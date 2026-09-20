"""A weak match is flagged, not matched — and a better one can take over.

Every figure, merchant and receipt in this module is invented — see
``tests/fixtures/README.md``.

A pair can be far too weak to be a reconciliation and still be the best thing
on offer at the moment it is scored. Storing such a pair as MATCHED counts it
as reconciled *and* takes the transaction out of the candidate pool, so the
receipt that really proves the row can never be found on any later run. That is
why a weak pair is flagged rather than matched, and why a strictly better
candidate has to be able to take the transaction off it.

Three layers are pinned here:

* ``classify_match`` — is this pair a reconciliation or only a suggestion?
* ``_TxnClaims`` — inside one run, the better pair wins the transaction
  regardless of which worker finished first, and by a total order, so the same
  inputs always produce the same holder.
* ``find_matches`` — a flagged pair comes back PENDING_REVIEW with the
  transaction left UNMATCHED (so it stays in the pool), and a strictly better
  candidate takes the transaction off it, freeing the weak receipt.

The scenario's numbers are *derived*, not quoted: the sub-scores below are
computed from the invented amounts, dates and strings by the engine's own
scorers, and ``TestFindMatchesFloor`` then asserts a whole run reproduces them.
Change an amount or a date at the top and every expectation follows.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from core.reconciliation import (
    DISPLACEMENT_MARGIN,
    MATCH_CONFIDENCE_FLOOR,
    VENDOR_SCORE_NO_SIGNAL,
    _description_names_other_vendor,
    _Proposal,
    _TxnClaims,
    classify_match,
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
    non-deterministic answer in tests whose whole subject is determinism.
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


# ── The invented pair the rules below are argued over ────────────────────
#
# Example Corp Inc. buys office sundries from an online retailer and pays a
# telecom bill in the same week. The bank line names the retailer; the receipt
# in the folder is the telecom invoice, for a different amount, four days later.

RETAILER_DESC = "EXAMPLE RETAILER MKTP CA"
RETAILER_TOTAL = Decimal("104.55")
RETAILER_DATE = date(2026, 4, 14)

TELECOM_VENDOR = "Orchid Telecom"
TELECOM_TOTAL = Decimal("120.00")
TELECOM_DATE = date(2026, 4, 18)
TELECOM_FILE = "20260418-orchid-telecom-invoice.pdf"

RETAILER_TXN_ID = 412
TELECOM_DOC_ID = 88
RETAILER_DOC_ID = 293

# What the engine's own scorers make of the wrong pair. $15.45 apart is inside
# the wire-fee window the relaxed same-currency comparison keeps open (0.65),
# four days apart is the 4–7 day bucket (0.7), and the vendor sub-score is
# whatever rapidfuzz makes of two names with nothing in common.
WEAK_AMOUNT_SCORE = 0.65
WEAK_DATE_SCORE = compute_date_score(RETAILER_DATE, TELECOM_DATE)
WEAK_VENDOR_SCORE = compute_vendor_score(RETAILER_DESC, TELECOM_VENDOR, TELECOM_FILE)
WEAK_CONFIDENCE = compute_confidence(
    WEAK_AMOUNT_SCORE, WEAK_DATE_SCORE, WEAK_VENDOR_SCORE,
)

# And of the right pair: the same figure, the same day, the retailer named on
# the bank line.
STRONG_CONFIDENCE = compute_confidence(
    1.0, 1.0, compute_vendor_score(RETAILER_DESC, "Example Retailer"),
)


def _txn(
    *, id: int = RETAILER_TXN_ID, amount: Decimal = -RETAILER_TOTAL,
    description: str = RETAILER_DESC, posted: date = RETAILER_DATE,
    currency: str = "CAD",
) -> Transaction:
    return Transaction(
        id=id, account=AccountType.CAD, transaction_type="DEBIT",
        date_posted=posted, amount=amount, currency=currency,
        description=description, source_file="statement_apr2026_cad.csv",
        source_row=1, status=TransactionStatus.UNMATCHED,
    )


def _doc(
    *, id: int, total: Decimal, vendor: str, doc_date: date,
    filename: str | None = None, currency: str = "CAD",
) -> Document:
    return Document(
        id=id, original_filename=filename or f"{vendor.lower()}-{id}.pdf",
        file_hash=f"{id:032d}", vendor=vendor, document_date=doc_date,
        currency=currency, total=total, status=DocumentStatus.EXTRACTED,
    )


def _telecom_doc() -> Document:
    """The weak candidate: a receipt that does not prove the retailer's row."""
    return _doc(
        id=TELECOM_DOC_ID, total=TELECOM_TOTAL, vendor=TELECOM_VENDOR,
        doc_date=TELECOM_DATE, filename=TELECOM_FILE,
    )


def _retailer_doc() -> Document:
    """The receipt that actually proves the retailer's row."""
    return _doc(
        id=RETAILER_DOC_ID, total=RETAILER_TOTAL, vendor="Example Retailer",
        doc_date=RETAILER_DATE, filename="20260414-example-retailer-receipt.pdf",
    )


def test_the_invented_scenario_is_the_shape_the_rest_of_the_file_assumes():
    """The weak pair is under the floor and the strong pair is over it.

    Asserted once, here, where the reason is written down — so a change to the
    scorers or to the floor fails on this sentence instead of on nine
    expectations further down that read like arithmetic errors.
    """
    assert WEAK_CONFIDENCE < MATCH_CONFIDENCE_FLOOR
    assert STRONG_CONFIDENCE >= MATCH_CONFIDENCE_FLOOR
    # The wrong pair is only weak, not absurd: it agrees on nothing but an
    # amount close enough to look like a bank fee, which is what makes it
    # storable at all and therefore worth a rule.
    assert WEAK_AMOUNT_SCORE > 0.0


# ── classify_match ────────────────────────────────────────────────────────

class TestClassifyMatch:
    def test_the_weak_contradicted_pair_is_flagged(self):
        result = classify_match(
            confidence=WEAK_CONFIDENCE, vendor_score=WEAK_VENDOR_SCORE,
            txn_description=RETAILER_DESC, doc_vendor=TELECOM_VENDOR,
            doc_filename=TELECOM_FILE,
        )
        assert result.flagged
        assert not result.settled
        # Both reasons fire: under the floor AND a contradicted vendor.
        assert len(result.reasons) == 2
        assert "below the" in result.reason_text()
        assert TELECOM_VENDOR in result.reason_text()

    def test_the_vendor_sub_score_is_a_weak_signal_not_a_clean_zero(self):
        """A weak signal, not a clean zero — which is why the gate matters.

        The two names share an e, an a, an l and an r, and rapidfuzz scores
        that incidental overlap well above ``VENDOR_SCORE_NO_SIGNAL``. A
        contradiction rule gated on "the vendor sub-score is zero" would
        therefore never look at a pair like this one at all.
        """
        assert WEAK_VENDOR_SCORE > VENDOR_SCORE_NO_SIGNAL
        assert WEAK_VENDOR_SCORE < 0.30

    def test_strong_pair_is_settled(self):
        result = classify_match(
            confidence=STRONG_CONFIDENCE, vendor_score=0.8,
            txn_description=RETAILER_DESC, doc_vendor="Example Retailer",
            doc_filename="20260414-example-retailer-receipt.pdf",
        )
        assert result.settled
        assert result.reasons == ()

    def test_confident_pair_with_contradicted_vendor_is_still_flagged(self):
        """Amount and date can agree by coincidence — the vendor decides."""
        result = classify_match(
            confidence=0.80, vendor_score=0.0,
            txn_description="ORCHID TELECOM PREAUTH PMT",
            doc_vendor="Bluepeak Software Inc",
            doc_filename="20260411-bluepeak-software.pdf",
        )
        assert result.flagged
        assert len(result.reasons) == 1

    def test_opaque_description_is_not_treated_as_a_contradiction(self):
        """An opaque description names nobody, so a 0 vendor score means nothing."""
        result = classify_match(
            confidence=0.85, vendor_score=0.0,
            txn_description="POS PURCHASE 0000",
            doc_vendor="Cedarview Supplies Ltd",
            doc_filename="20260409-cedarview-supplies.pdf",
        )
        assert result.settled

    def test_payment_intermediary_is_not_a_contradiction(self):
        """A transfer-platform line names the platform, not the payee."""
        result = classify_match(
            confidence=0.75, vendor_score=0.0,
            txn_description="WISEMSP 1800.00 OUTGOING WIRE",
            doc_vendor="Jordan Avery Design",
            doc_filename="20260312-jordan-avery-invoice.pdf",
        )
        assert result.settled

    def test_receipt_with_no_vendor_is_judged_on_confidence_alone(self):
        assert classify_match(
            confidence=0.85, vendor_score=0.0,
            txn_description=RETAILER_DESC, doc_vendor=None, doc_filename="",
        ).settled
        assert classify_match(
            confidence=0.40, vendor_score=0.0,
            txn_description=RETAILER_DESC, doc_vendor=None, doc_filename="",
        ).flagged

    def test_floor_is_the_documented_review_threshold(self):
        from config.settings import reconciliation_config

        assert MATCH_CONFIDENCE_FLOOR == reconciliation_config.review_threshold
        just_under = classify_match(
            confidence=MATCH_CONFIDENCE_FLOOR - 0.01, vendor_score=0.8,
            txn_description=RETAILER_DESC, doc_vendor="Example Retailer",
        )
        just_over = classify_match(
            confidence=MATCH_CONFIDENCE_FLOOR, vendor_score=0.8,
            txn_description=RETAILER_DESC, doc_vendor="Example Retailer",
        )
        assert just_under.flagged
        assert just_over.settled


# ── _TxnClaims: the better pair wins, and the same pair wins every time ───

def _match(txn_id: int, doc_id: int, confidence: float) -> ReconciliationMatch:
    return ReconciliationMatch(
        transaction_id=txn_id, document_id=doc_id, confidence_score=confidence,
        amount_score=1.0, date_score=1.0, vendor_score=0.5,
        match_type=MatchType.ONE_TO_ONE, status=MatchStatus.PENDING_REVIEW,
    )


def _proposal(
    txn_id: int, doc_id: int, confidence: float, flagged: bool,
    amount_score: float = 1.0, date_gap: int = 0,
) -> _Proposal:
    return _Proposal(
        doc_id=doc_id, txn_id=txn_id, confidence=confidence, flagged=flagged,
        amount_score=amount_score, date_gap=date_gap,
        match=_match(txn_id, doc_id, confidence),
    )


class TestTxnClaims:
    def test_the_only_proposal_holds_the_transaction(self):
        claims = _TxnClaims()
        claims.propose(_proposal(RETAILER_TXN_ID, TELECOM_DOC_ID, 0.59, True))
        winners, losers = claims.resolve()
        assert [(w.txn_id, w.doc_id) for w in winners] == [
            (RETAILER_TXN_ID, TELECOM_DOC_ID)
        ]
        assert losers == []
        assert [m.document_id for m in claims.matches()] == [TELECOM_DOC_ID]

    def test_flagged_claim_yields_to_a_clearly_better_pair(self):
        claims = _TxnClaims()
        claims.propose(_proposal(RETAILER_TXN_ID, TELECOM_DOC_ID, 0.59, True))
        claims.propose(_proposal(RETAILER_TXN_ID, RETAILER_DOC_ID, 0.96, False))
        winners, losers = claims.resolve()
        assert [w.doc_id for w in winners] == [RETAILER_DOC_ID]
        assert [loser.doc_id for loser in losers] == [TELECOM_DOC_ID]
        # Only the winner survives, so the telecom invoice ends the run
        # unmatched and stays available to the row it really belongs to.
        assert [m.document_id for m in claims.matches()] == [RETAILER_DOC_ID]

    def test_a_flagged_pair_never_takes_a_transaction_off_a_settled_one(self):
        """Even at a higher confidence: flagged means something is wrong with it."""
        claims = _TxnClaims()
        # vendor-contradicted, but scored high
        claims.propose(_proposal(RETAILER_TXN_ID, TELECOM_DOC_ID, 0.99, True))
        # clean, but scored lower
        claims.propose(_proposal(RETAILER_TXN_ID, RETAILER_DOC_ID, 0.70, False))
        winners, _losers = claims.resolve()
        assert [w.doc_id for w in winners] == [RETAILER_DOC_ID]

    def test_no_margin_is_needed_inside_a_run(self):
        """Two sub-floor candidates: the better one wins, by however little.

        DISPLACEMENT_MARGIN guards an incumbent against scoring noise. Inside
        one run there is no incumbent — every competitor is ranked at the same
        moment — so the margin does not apply here. Where it does apply is
        ``_store_matches``, against a match stored by an earlier run.
        """
        claims = _TxnClaims()
        claims.propose(_proposal(RETAILER_TXN_ID, TELECOM_DOC_ID, 0.50, True))
        claims.propose(_proposal(
            RETAILER_TXN_ID, RETAILER_DOC_ID, 0.50 + DISPLACEMENT_MARGIN - 0.01, True,
        ))
        winners, _losers = claims.resolve()
        assert [w.doc_id for w in winners] == [RETAILER_DOC_ID]

    def test_the_order_proposals_arrive_in_changes_nothing(self):
        """The whole point: the Review queue shows the same suggestion every run."""
        proposals = [
            _proposal(RETAILER_TXN_ID, TELECOM_DOC_ID, 0.58, True,
                      amount_score=0.9, date_gap=4),
            _proposal(RETAILER_TXN_ID, RETAILER_DOC_ID, 0.58, True,
                      amount_score=0.9, date_gap=2),
            _proposal(RETAILER_TXN_ID, 41, 0.54, True, amount_score=1.0, date_gap=0),
            _proposal(413, 12, 0.62, False),
        ]
        holders = set()
        for order in itertools.permutations(range(len(proposals))):
            claims = _TxnClaims()
            for i in order:
                claims.propose(proposals[i])
            winners, _losers = claims.resolve()
            holders.add(tuple(sorted((w.txn_id, w.doc_id) for w in winners)))
        # The retailer receipt takes the retailer row on the smaller date gap
        # at equal confidence; a higher amount score cannot rescue the third
        # candidate, because confidence is compared first. The last pair is
        # unopposed and simply holds.
        assert holders == {((RETAILER_TXN_ID, RETAILER_DOC_ID), (413, 12))}

    def test_ties_are_broken_by_the_lower_document_id(self):
        """The key's last component is unique, so nothing is ever a real tie."""
        claims = _TxnClaims()
        claims.propose(_proposal(RETAILER_TXN_ID, 900, 0.58, True,
                                 amount_score=0.9, date_gap=3))
        claims.propose(_proposal(RETAILER_TXN_ID, 42, 0.58, True,
                                 amount_score=0.9, date_gap=3))
        winners, _losers = claims.resolve()
        assert [w.doc_id for w in winners] == [42]

    def test_a_resolved_transaction_is_held_against_the_next_round(self):
        claims = _TxnClaims()
        claims.propose(_proposal(RETAILER_TXN_ID, TELECOM_DOC_ID, 0.59, True))
        claims.resolve()
        assert claims.held_txn_ids() == {RETAILER_TXN_ID}
        assert claims.held_doc_ids() == {TELECOM_DOC_ID}

    def test_matches_come_back_best_first(self):
        claims = _TxnClaims()
        claims.propose(_proposal(560, 11, 0.55, True))
        claims.propose(_proposal(561, 12, 0.95, False))
        claims.propose(_proposal(562, 13, 0.75, False))
        claims.resolve()
        assert [m.document_id for m in claims.matches()] == [12, 13, 11]


# ── find_matches: the whole scenario, end to end ──────────────────────────

class TestFindMatchesFloor:
    @pytest.mark.parametrize("doc_order", [0, 1])
    def test_the_retailer_receipt_wins_the_retailer_row_either_order(self, doc_order):
        """Order independence: the run must not depend on which worker finishes."""
        txn = _txn()
        docs = [_telecom_doc(), _retailer_doc()]
        if doc_order:
            docs.reverse()

        matches = find_matches([txn], docs)

        assert len(matches) == 1
        assert matches[0].document_id == RETAILER_DOC_ID
        assert matches[0].transaction_id == RETAILER_TXN_ID
        assert matches[0].status == MatchStatus.AUTO_APPROVED
        assert matches[0].confidence_score == pytest.approx(STRONG_CONFIDENCE)
        assert matches[0].confidence_score >= MATCH_CONFIDENCE_FLOOR

    def test_weak_only_candidate_is_returned_but_not_auto_approved(self):
        """With no retailer receipt, the telecom pair is a review item, not a match."""
        txn = _txn()
        matches = find_matches([txn], [_telecom_doc()])

        assert len(matches) == 1
        weak = matches[0]
        assert weak.document_id == TELECOM_DOC_ID
        # The run reproduces the sub-scores this module derived at the top.
        assert weak.amount_score == WEAK_AMOUNT_SCORE
        assert weak.date_score == WEAK_DATE_SCORE
        assert weak.vendor_score == pytest.approx(WEAK_VENDOR_SCORE)
        assert weak.confidence_score == pytest.approx(WEAK_CONFIDENCE)
        assert weak.confidence_score < MATCH_CONFIDENCE_FLOOR
        assert weak.status == MatchStatus.PENDING_REVIEW
        assert classify_match(
            weak.confidence_score, weak.vendor_score, RETAILER_DESC, TELECOM_VENDOR,
        ).flagged


class TestFindMatchesIsOrderIndependent:
    """Three sub-floor candidates for one opaque bank row, six document orders.

    Three receipts from the same supplier for the same amount, one bank line
    that names nobody. Everything about the three pairs scores identically
    except how far the receipt's date is from the bank line's.

    Nothing here clears MATCH_CONFIDENCE_FLOOR, so every pair is flagged. Under
    a first-come rule whichever worker finished first would keep the
    transaction unless a challenger beat it by DISPLACEMENT_MARGIN — which
    three candidates a few points apart never do — and the Review queue would
    change its mind between runs. A total order is what makes the suggestion
    the same every time.
    """

    OPAQUE = "POS PURCHASE 0000"
    SUPPLIER = "Cedarview Supplies Ltd"
    TXN_AMOUNT = Decimal("-300.00")
    DOC_TOTAL = Decimal("301.20")
    TXN_DATE = date(2026, 6, 10)

    def _pos_txn(self, id: int = 700, posted: date | None = None) -> Transaction:
        return _txn(
            id=id, amount=self.TXN_AMOUNT, description=self.OPAQUE,
            posted=posted or self.TXN_DATE,
        )

    def _docs(self) -> list[Document]:
        # 30, 25 and 40 days before the bank line: all inside the 22–45 day
        # bucket, so the date *score* cannot separate them either.
        return [
            _doc(id=501, total=self.DOC_TOTAL, vendor=self.SUPPLIER,
                 doc_date=date(2026, 5, 11), filename="cedarview-supplies-a.pdf"),
            _doc(id=502, total=self.DOC_TOTAL, vendor=self.SUPPLIER,
                 doc_date=date(2026, 5, 16), filename="cedarview-supplies-b.pdf"),
            _doc(id=503, total=self.DOC_TOTAL, vendor=self.SUPPLIER,
                 doc_date=date(2026, 5, 1), filename="cedarview-supplies-c.pdf"),
        ]

    def test_the_same_receipt_holds_the_row_in_every_document_order(self):
        holders = set()
        for order in itertools.permutations(self._docs()):
            matches = find_matches([self._pos_txn()], list(order))
            holders.add(tuple(sorted(
                (m.transaction_id, m.document_id) for m in matches
            )))
        assert len(holders) == 1, holders
        (only,) = holders
        # One transaction, one holder — the two losers end the run unmatched.
        assert len(only) == 1
        (txn_id, doc_id), = only
        assert txn_id == 700
        # All three score the same — a $1.20 amount difference, an opaque
        # description, and a date gap anywhere in the 22–45 day band — so
        # neither confidence nor the amount score separates them, and the date
        # gap does: 502 at 25 days, against 501 at 30 and 503 at 40.
        assert doc_id == 502

    def test_every_pair_here_really_is_below_the_floor(self):
        """Otherwise the test would be exercising the settled path by accident."""
        expected = compute_confidence(
            0.8,                                    # $1.20 apart, inside $2.00
            0.2,                                    # 22–45 days
            compute_vendor_score(self.OPAQUE, self.SUPPLIER),
        )
        assert expected < MATCH_CONFIDENCE_FLOOR
        for doc in self._docs():
            matches = find_matches([self._pos_txn()], [doc])
            assert len(matches) == 1
            assert matches[0].confidence_score == pytest.approx(expected)
            assert matches[0].confidence_score < MATCH_CONFIDENCE_FLOOR

    def test_a_loser_gets_a_second_look_at_what_is_left(self):
        """Two receipts, two near-identical bank rows: both get matched.

        The document that loses a contested transaction is re-run against the
        pool the settled round left behind, so losing one row does not cost it
        the row it is the only candidate for.
        """
        rows = [
            self._pos_txn(id=700, posted=date(2026, 6, 10)),
            self._pos_txn(id=701, posted=date(2026, 6, 11)),
        ]
        docs = [
            _doc(id=501, total=self.DOC_TOTAL, vendor=self.SUPPLIER,
                 doc_date=date(2026, 6, 9), filename="cedarview-supplies.pdf"),
            _doc(id=502, total=self.DOC_TOTAL, vendor="Meridian Courier",
                 doc_date=date(2026, 6, 9), filename="meridian-courier.pdf"),
        ]
        matched_docs = {m.document_id for m in find_matches(rows, docs)}
        assert matched_docs == {501, 502}


class TestAmountEvidenceRule:
    """No amount agreement is not a match, whatever the LLM's confidence says.

    A model shortlisting a ``WISE TRANSFER -$1,800.00 CAD`` row for a
    ``US$1,800.00`` contractor invoice can return a comfortable 0.70 for a pair
    whose deterministic amount score is 0.0, because an implied rate of 1.0000
    is outside every CAD/USD band. Confidence from one place and evidence from
    another is what this rule refuses to settle on.
    """

    KWARGS = dict(
        txn_description="WISE TRANSFER",
        doc_vendor="Jordan Avery Design",
        doc_filename="20260312-jordan-avery-invoice.pdf",
    )

    def test_llm_confidence_cannot_settle_a_pair_with_no_amount_evidence(self):
        result = classify_match(
            confidence=0.70, vendor_score=0.24, amount_score=0.0, **self.KWARGS,
        )
        assert result.flagged
        assert "no amount agreement" in result.reason_text()

    def test_a_real_amount_agreement_still_settles(self):
        result = classify_match(
            confidence=0.70, vendor_score=0.24, amount_score=0.78, **self.KWARGS,
        )
        assert result.settled

    def test_the_rule_is_opt_in_for_callers_without_a_sub_score(self):
        result = classify_match(confidence=0.70, vendor_score=0.24, **self.KWARGS)
        assert result.settled


# ── The vendor contradiction is asked at every vendor sub-score ───────────
#
# A ``CEDARVIEW SUPPLIES`` bank line against an Orchid Telecom invoice names
# two different parties, but the two names share an e, a c, an l and an s, and
# that incidental letter overlap is enough to lift rapidfuzz off zero. A rule
# gated on `vendor_score <= 0.05` would skip the check and let the pair settle.
# The rule runs at any vendor sub-score, and says who it thinks the bank line
# named.


class TestVendorContradiction:
    def test_the_retailer_telecom_pair_is_flagged_with_both_names(self):
        result = classify_match(
            confidence=0.67,
            vendor_score=compute_vendor_score(RETAILER_DESC, TELECOM_VENDOR),
            txn_description=RETAILER_DESC, doc_vendor=TELECOM_VENDOR,
            doc_filename=TELECOM_FILE,
            amount_score=1.0,
        )
        assert result.flagged
        # Above the floor and with a perfect amount, so the contradiction is the
        # only thing standing between this pair and a settled match.
        assert result.reasons == (
            f"description names EXAMPLE RETAILER, receipt is from {TELECOM_VENDOR}",
        )

    def test_incidental_letter_overlap_lifts_the_vendor_score_off_zero(self):
        """Two unrelated names still score above a clean zero, so a rule gated
        on a zero sub-score would never examine this pair."""
        score = compute_vendor_score(RETAILER_DESC, TELECOM_VENDOR)
        assert score > VENDOR_SCORE_NO_SIGNAL
        assert score < 0.3

    def test_a_retailer_bank_line_against_a_retailer_receipt_is_no_contradiction(self):
        result = classify_match(
            confidence=0.88, vendor_score=0.8,
            txn_description=RETAILER_DESC, doc_vendor="Example Retailer",
            doc_filename="20260414-example-retailer-receipt.pdf", amount_score=1.0,
        )
        assert result.settled

    def test_an_abbreviated_merchant_is_the_same_party(self):
        """"EXMPL MKTP CA" and "Example Retailer" are one merchant, not two.

        The acquirer tag and the reference code are stripped as noise, and
        "exmpl" is close enough to "example" that the rule refuses to call it a
        different party.
        """
        result = classify_match(
            confidence=0.82, vendor_score=0.31,
            txn_description="EXMPL MKTP CA*7F2K9", doc_vendor="Example Retailer",
            doc_filename="20260414-example-retailer-receipt.pdf", amount_score=1.0,
        )
        assert result.settled

    def test_a_processor_prefixed_line_naming_the_real_vendor_is_no_contradiction(
        self,
    ):
        """"PAYPAL *ORCHID TELECOM" names the rail and then the payee."""
        result = classify_match(
            confidence=0.79, vendor_score=0.8,
            txn_description="PAYPAL *ORCHID TELECOM", doc_vendor=TELECOM_VENDOR,
            doc_filename=TELECOM_FILE, amount_score=1.0,
        )
        assert result.settled

    def test_a_rail_only_line_names_nobody_and_stays_neutral(self):
        """A transfer-platform line says the platform whoever the payee was."""
        for vendor in (
            TELECOM_VENDOR, "Jordan Avery Design", "Cedarview Supplies Ltd", "Nim",
        ):
            assert classify_match(
                confidence=0.75, vendor_score=0.24,
                txn_description="WISE TRANSFER", doc_vendor=vendor,
                doc_filename=f"{vendor}.pdf", amount_score=0.9,
            ).settled, vendor

    def test_a_processor_line_naming_a_third_party_is_still_a_contradiction(self):
        """Strip the rail and CEDARVIEW SUPPLIES is still not Orchid Telecom."""
        result = classify_match(
            confidence=0.71, vendor_score=0.2,
            txn_description="PAYPAL *CEDARVIEW SUPPLIES", doc_vendor=TELECOM_VENDOR,
            doc_filename=TELECOM_FILE, amount_score=1.0,
        )
        assert result.flagged
        assert f"CEDARVIEW SUPPLIES, receipt is from {TELECOM_VENDOR}" in (
            result.reason_text()
        )

    def test_an_opaque_line_with_no_merchant_is_never_a_contradiction(self):
        for description in (
            "POS PURCHASE 0000",
            "MONTHLY FEE",
            "INTERAC E-TRANSFER SENT",
            "SERVICE CHARGE",
        ):
            assert classify_match(
                confidence=0.85, vendor_score=0.0,
                txn_description=description, doc_vendor="Cedarview Supplies Ltd",
                doc_filename="20260409-cedarview-supplies.pdf", amount_score=1.0,
            ).settled, description

    def test_a_high_vendor_score_cannot_hide_a_contradiction_either(self):
        """The gate is gone in both directions: the names decide, not the score."""
        result = classify_match(
            confidence=0.92, vendor_score=0.55,
            txn_description="CEDARVIEW SUPPLIES #0000", doc_vendor=TELECOM_VENDOR,
            doc_filename=TELECOM_FILE, amount_score=1.0,
        )
        assert result.flagged

    def test_the_reported_merchant_is_the_merchant_not_the_whole_bank_line(self):
        """Reference numbers and rail tags are not part of the name reported."""
        result = classify_match(
            confidence=0.80, vendor_score=0.0,
            txn_description="ORCHID TELECOM PREAUTH PMT 0000000",
            doc_vendor="Bluepeak Software Inc",
            doc_filename="20260411-bluepeak-software.pdf", amount_score=1.0,
        )
        assert result.reasons == (
            "description names ORCHID TELECOM, receipt is from Bluepeak Software Inc",
        )

    def test_a_vendor_a_rule_already_names_is_reported_by_that_name(self):
        """A name the vendor tables recognise is reported as they spell it.

        ``config/vendor_rules.yaml`` ships ``SAMPLE STUDIOS (LTD|INC)?`` as an
        example rule, so "SAMPLE STUDIOS DESIGN 0000000" is a line that names a
        vendor the shipped rules already know. Without a known-vendor hit the
        rule reports the first few merchant words of the bank line (the test
        above); with one it reports the canonical name it recognised, which is
        shorter and truer.
        """
        result = classify_match(
            confidence=0.80, vendor_score=0.0,
            txn_description="SAMPLE STUDIOS DESIGN 0000000",
            doc_vendor="Bluepeak Software Inc",
            doc_filename="20260411-bluepeak-software.pdf", amount_score=1.0,
        )
        assert result.reasons == (
            "description names SAMPLE STUDIOS, receipt is from Bluepeak Software Inc",
        )

    def test_a_receipt_with_no_vendor_contradicts_nothing(self):
        assert classify_match(
            confidence=0.85, vendor_score=0.0,
            txn_description="CEDARVIEW SUPPLIES #0000", doc_vendor=None,
            doc_filename="", amount_score=1.0,
        ).settled

    def test_the_underlying_question_is_still_available_as_a_boolean(self):
        assert _description_names_other_vendor(
            RETAILER_DESC, TELECOM_VENDOR,
        ) is True
        assert _description_names_other_vendor(
            RETAILER_DESC, "Example Retailer",
        ) is False
        assert _description_names_other_vendor(
            "WISE TRANSFER", "Jordan Avery Design",
        ) is False
