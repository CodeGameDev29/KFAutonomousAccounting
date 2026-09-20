"""Storage half: what a run is allowed to write, and what it may take back.

``server.api.reconciliation._store_matches`` is where a pair well below the
confidence floor could turn into a MATCHED transaction — and a MATCHED row drops
out of every later candidate pool, so it could never reach the receipt that
really proves it. These tests drive that function against a fake database and
assert the four outcomes it has to produce:

1. a settled pair is stored and both sides marked MATCHED;
2. a flagged pair is stored PENDING_REVIEW and both sides are **left in the
   pool**;
3. a strictly better candidate displaces a stored flagged match, and the
   displaced receipt goes back to unmatched;
4. a candidate that is not better by the documented margin — or that faces a
   human's decision — is not stored at all.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.reconciliation import DISPLACEMENT_MARGIN
from models.document import Document, DocumentStatus
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, Transaction, TransactionStatus
from server.api.reconciliation import _store_matches

THRESHOLD = 0.90


class FakeDb:
    """Just the surface ``_store_matches`` touches."""

    def __init__(self, existing: list[dict] | None = None) -> None:
        self.existing = existing or []
        self.inserted: list[ReconciliationMatch] = []
        self.txn_status: dict[int, str] = {}
        self.doc_status: dict[int, str] = {}
        self.superseded: list[tuple[int, str]] = []
        self.refreshed: list[tuple] = []
        self._next_id = 9000

    # -- reads
    def get_active_matches_for(self, transaction_ids, document_ids) -> list[dict]:
        return [
            row for row in self.existing
            if row["transaction_id"] in set(transaction_ids)
            or row["document_id"] in set(document_ids)
        ]

    # -- writes
    def insert_match(self, match) -> int:
        self.inserted.append(match)
        self._next_id += 1
        return self._next_id

    def update_transaction_status(self, txn_id, status) -> None:
        self.txn_status[txn_id] = status

    def update_document_status(self, doc_id, status) -> None:
        self.doc_status[doc_id] = status

    def supersede_match(self, match_id, reason) -> dict:
        self.superseded.append((match_id, reason))
        for row in self.existing:
            if row["id"] == match_id:
                self.doc_status[row["document_id"]] = "EXTRACTED"
                self.txn_status[row["transaction_id"]] = "UNMATCHED"
                return {"match_id": match_id, **row}
        return {"match_id": match_id}

    def refresh_match_scores(self, *args) -> None:
        self.refreshed.append(args)


def _txn(id=101, amount=Decimal("-41.25"), description="EXAMPLE RETAILER") -> Transaction:
    return Transaction(
        id=id, account=AccountType.CAD, transaction_type="DEBIT",
        date_posted=date(2026, 1, 15), amount=amount, currency="CAD",
        description=description, source_file="test_bank_cad.csv", source_row=1,
        status=TransactionStatus.UNMATCHED,
    )


def _doc(id, total, vendor, filename) -> Document:
    return Document(
        id=id, original_filename=filename, file_hash=f"h{id}", vendor=vendor,
        document_date=date(2026, 1, 15), currency="CAD", total=total,
        status=DocumentStatus.EXTRACTED,
    )


def _match(txn_id, doc_id, confidence, vendor_score=0.8) -> ReconciliationMatch:
    return ReconciliationMatch(
        transaction_id=txn_id, document_id=doc_id, confidence_score=confidence,
        amount_score=1.0, date_score=1.0, vendor_score=vendor_score,
        match_type=MatchType.ONE_TO_ONE, status=MatchStatus.PENDING_REVIEW,
    )


TELECOM_DOC = _doc(
    3, Decimal("56.00"), "ORCHID TELECOM", "orchid-telecom-invoice-2026-01-19.pdf",
)
RETAILER_DOC = _doc(
    2, Decimal("41.25"), "Example Retailer", "test_receipt_retailer.pdf",
)


def _maps(*docs):
    return {1: _txn()}, {d.id: d for d in docs}


class TestSettledPair:
    def test_stored_and_both_sides_matched(self):
        db = FakeDb()
        txn_map, doc_map = _maps(RETAILER_DOC)
        counts = _store_matches(db, [_match(1, 2, 0.96)], txn_map, doc_map, THRESHOLD)

        assert counts["stored"] == 1
        assert counts["auto_approved"] == 1
        assert counts["flagged"] == 0
        assert db.txn_status[1] == "MATCHED"
        assert db.doc_status[2] == "MATCHED"
        assert db.inserted[0].status == MatchStatus.AUTO_APPROVED

    def test_below_auto_approve_but_above_the_floor_is_still_a_match(self):
        db = FakeDb()
        txn_map, doc_map = _maps(RETAILER_DOC)
        counts = _store_matches(db, [_match(1, 2, 0.75)], txn_map, doc_map, THRESHOLD)

        assert counts["stored"] == 1
        assert counts["pending_review"] == 1
        assert counts["flagged"] == 0
        assert db.txn_status[1] == "MATCHED"


class TestFlaggedPair:
    def test_weak_pair_is_flagged_and_left_in_the_pool(self):
        """A pair under the floor may be recorded, but it may not consume a row."""
        db = FakeDb()
        txn_map, doc_map = _maps(TELECOM_DOC)
        counts = _store_matches(
            db, [_match(1, 3, 0.535, vendor_score=0.0)], txn_map, doc_map, THRESHOLD,
        )

        assert counts["stored"] == 1
        assert counts["flagged"] == 1
        assert counts["auto_approved"] == 0
        assert db.inserted[0].status == MatchStatus.PENDING_REVIEW
        # The whole point: neither side is marked, so both stay candidates.
        assert db.txn_status == {}
        assert db.doc_status == {}

    def test_flag_reason_leads_the_explanation(self):
        db = FakeDb()
        txn_map, doc_map = _maps(TELECOM_DOC)
        _store_matches(
            db, [_match(1, 3, 0.535, vendor_score=0.0)], txn_map, doc_map, THRESHOLD,
        )
        explanation = db.inserted[0].explanation
        assert explanation.startswith("Flagged for review")
        assert "ORCHID TELECOM" in explanation


class TestDisplacement:
    def _weak_existing(self, confidence=0.54, status="PENDING_REVIEW", source="AUTO"):
        return [{
            "id": 4, "transaction_id": 1, "document_id": 3,
            "confidence_score": confidence, "status": status,
            "match_source": source, "match_type": "ONE_TO_ONE",
        }]

    def test_better_candidate_displaces_and_frees_the_receipt(self):
        db = FakeDb(self._weak_existing())
        txn_map, doc_map = _maps(TELECOM_DOC, RETAILER_DOC)
        counts = _store_matches(db, [_match(1, 2, 0.96)], txn_map, doc_map, THRESHOLD)

        assert counts["displaced"] == 1
        assert counts["stored"] == 1
        assert db.superseded[0][0] == 4
        assert "Superseded" in db.superseded[0][1]
        # Displaced receipt back in the pool; the winner is matched.
        assert db.doc_status[3] == "EXTRACTED"
        assert db.doc_status[2] == "MATCHED"
        assert db.txn_status[1] == "MATCHED"

    def test_challenger_inside_the_margin_is_not_stored(self):
        db = FakeDb(self._weak_existing(confidence=0.54))
        txn_map, doc_map = _maps(TELECOM_DOC, RETAILER_DOC)
        counts = _store_matches(
            db, [_match(1, 2, 0.54 + DISPLACEMENT_MARGIN - 0.01)],
            txn_map, doc_map, THRESHOLD,
        )

        assert counts["displaced"] == 0
        assert counts["stored"] == 0
        assert counts["not_stored"] == 1
        assert db.inserted == []

    def test_settled_existing_match_is_not_displaced(self):
        db = FakeDb(self._weak_existing(confidence=0.80))
        txn_map, doc_map = _maps(TELECOM_DOC, RETAILER_DOC)
        counts = _store_matches(db, [_match(1, 2, 0.99)], txn_map, doc_map, THRESHOLD)

        assert counts["stored"] == 0
        assert counts["displaced"] == 0

    def test_a_humans_manual_match_is_never_displaced(self):
        db = FakeDb(self._weak_existing(confidence=0.20, source="MANUAL"))
        txn_map, doc_map = _maps(TELECOM_DOC, RETAILER_DOC)
        counts = _store_matches(db, [_match(1, 2, 0.99)], txn_map, doc_map, THRESHOLD)

        assert counts["stored"] == 0
        assert db.superseded == []

    def test_an_approved_match_is_never_displaced(self):
        db = FakeDb(self._weak_existing(confidence=0.20, status="USER_APPROVED"))
        txn_map, doc_map = _maps(TELECOM_DOC, RETAILER_DOC)
        counts = _store_matches(db, [_match(1, 2, 0.99)], txn_map, doc_map, THRESHOLD)

        assert counts["stored"] == 0
        assert db.superseded == []

    def test_a_receipt_already_matched_elsewhere_blocks_the_pair(self):
        existing = [{
            "id": 77, "transaction_id": 999, "document_id": 2,
            "confidence_score": 0.95, "status": "AUTO_APPROVED",
            "match_source": "AUTO", "match_type": "ONE_TO_ONE",
        }]
        db = FakeDb(existing)
        txn_map, doc_map = _maps(RETAILER_DOC)
        counts = _store_matches(db, [_match(1, 2, 0.97)], txn_map, doc_map, THRESHOLD)

        assert counts["stored"] == 0
        assert db.superseded == []

    def test_best_candidate_is_offered_the_transaction_first(self):
        """Two candidates for one row: the weaker one must not get there first."""
        db = FakeDb()
        txn_map, doc_map = _maps(TELECOM_DOC, RETAILER_DOC)
        counts = _store_matches(
            db,
            [_match(1, 3, 0.535, vendor_score=0.0), _match(1, 2, 0.96)],
            txn_map, doc_map, THRESHOLD,
        )

        assert counts["stored"] == 1
        assert db.inserted[0].document_id == 2
        assert db.txn_status[1] == "MATCHED"


class TestRepeatedRuns:
    def test_the_same_flagged_pair_is_re_scored_not_duplicated(self):
        """A flagged pair stays in the pool, so the next run proposes it again."""
        existing = [{
            "id": 4, "transaction_id": 1, "document_id": 3,
            "confidence_score": 0.535, "status": "PENDING_REVIEW",
            "match_source": "AUTO", "match_type": "ONE_TO_ONE",
        }]
        db = FakeDb(existing)
        txn_map, doc_map = _maps(TELECOM_DOC)
        counts = _store_matches(
            db, [_match(1, 3, 0.535, vendor_score=0.0)], txn_map, doc_map, THRESHOLD,
        )

        assert counts["refreshed"] == 1
        assert counts["stored"] == 0
        assert db.inserted == []
        assert db.refreshed[0][0] == 4
        assert db.txn_status == {}
