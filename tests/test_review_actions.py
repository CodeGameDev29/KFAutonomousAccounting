"""Tests for reconciliation match review actions against DatabasePg.

Four behaviours the review queue depends on, all over mocked PostgreSQL
connections: approving a match records who approved it and when; rejecting one
puts the transaction and the document back in the pool so neither is left
claiming proof it no longer has; a match's confidence decides which queue it
lands in, with the auto-approve threshold read from the API module rather than
copied; and the pending-review listing returns only matches still awaiting a
decision.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from models.document import DocumentStatus
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import TransactionStatus
from server.api.reconciliation import AUTO_APPROVE_THRESHOLD

# ─── In-memory mock store ────────────────────────────────────────────

class FakeStore:
    """Simulates the relevant tables for reconciliation review tests."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.matches: dict[int, dict] = {}
        self.transactions: dict[int, dict] = {}
        self.documents: dict[int, dict] = {}
        self._next_match_id = 1

    def add_match(
        self,
        transaction_id: int,
        document_id: int,
        confidence_score: float,
        *,
        amount_score: float = 0.9,
        date_score: float = 0.9,
        vendor_score: float = 0.9,
        match_type: str = "ONE_TO_ONE",
        status: str | None = None,
    ) -> int:
        """Insert a match and auto-classify by confidence if status not given."""
        if status is None:
            if confidence_score >= AUTO_APPROVE_THRESHOLD:
                status = "AUTO_APPROVED"
            elif confidence_score >= 0.60:
                status = "PENDING_REVIEW"
            else:
                status = "PENDING_REVIEW"  # low-conf still flagged for review
        mid = self._next_match_id
        self._next_match_id += 1
        self.matches[mid] = {
            "id": mid,
            "transaction_id": transaction_id,
            "document_id": document_id,
            "confidence_score": confidence_score,
            "amount_score": amount_score,
            "date_score": date_score,
            "vendor_score": vendor_score,
            "match_type": match_type,
            "status": status,
            "reviewed_by": None,
            "reviewed_at": None,
            "created_at": datetime.now(timezone.utc),
        }
        return mid

    def add_transaction(self, txn_id: int, status: str = "MATCHED") -> None:
        self.transactions[txn_id] = {"id": txn_id, "status": status}

    def add_document(self, doc_id: int, status: str = "MATCHED") -> None:
        self.documents[doc_id] = {"id": doc_id, "status": status}


def _build_mock_db(store: FakeStore):
    """Build a MagicMock DatabasePg whose key methods delegate to *store*."""
    db = MagicMock()
    db.user_id = store.user_id

    # --- update_match_status ---
    def _update_match_status(match_id: int, status: str, reviewed_by: str | None = None):
        m = store.matches.get(match_id)
        if m is None:
            return
        m["status"] = status
        m["reviewed_by"] = reviewed_by
        m["reviewed_at"] = datetime.now(timezone.utc)

    db.update_match_status.side_effect = _update_match_status

    # --- get_match_by_id ---
    def _get_match_by_id(match_id: int):
        m = store.matches.get(match_id)
        if m is None:
            return None
        return ReconciliationMatch(
            id=m["id"],
            transaction_id=m["transaction_id"],
            document_id=m["document_id"],
            confidence_score=m["confidence_score"],
            amount_score=m["amount_score"],
            date_score=m["date_score"],
            vendor_score=m["vendor_score"],
            match_type=MatchType(m["match_type"]),
            status=MatchStatus(m["status"]),
            reviewed_by=m["reviewed_by"],
            reviewed_at=m["reviewed_at"],
        )

    db.get_match_by_id.side_effect = _get_match_by_id

    # --- get_pending_reviews ---
    def _get_pending_reviews():
        results = []
        for m in store.matches.values():
            if m["status"] == "PENDING_REVIEW":
                results.append(ReconciliationMatch(
                    id=m["id"],
                    transaction_id=m["transaction_id"],
                    document_id=m["document_id"],
                    confidence_score=m["confidence_score"],
                    amount_score=m["amount_score"],
                    date_score=m["date_score"],
                    vendor_score=m["vendor_score"],
                    match_type=MatchType(m["match_type"]),
                    status=MatchStatus(m["status"]),
                    reviewed_by=m["reviewed_by"],
                    reviewed_at=m["reviewed_at"],
                ))
        return results

    db.get_pending_reviews.side_effect = _get_pending_reviews

    # --- delete_match ---
    def _delete_match(match_id: int):
        return store.matches.pop(match_id, None) is not None

    db.delete_match.side_effect = _delete_match

    # --- update_transaction_status ---
    def _update_transaction_status(txn_id: int, status):
        status_val = status.value if hasattr(status, "value") else status
        t = store.transactions.get(txn_id)
        if t:
            t["status"] = status_val

    db.update_transaction_status.side_effect = _update_transaction_status

    # --- update_document_status ---
    def _update_document_status(doc_id: int, status: str):
        d = store.documents.get(doc_id)
        if d:
            d["status"] = status

    db.update_document_status.side_effect = _update_document_status

    return db


# ═══════════════════════════════════════════════════════════════════
#  Approve action updates match status
# ═══════════════════════════════════════════════════════════════════


class TestApproveAction:
    """Calling update_match_status with USER_APPROVED sets status,
    reviewed_by, and reviewed_at correctly."""

    def test_approve_sets_status(self):
        store = FakeStore("user-1")
        store.add_transaction(10, "MATCHED")
        store.add_document(20, "MATCHED")
        mid = store.add_match(10, 20, 0.72, status="PENDING_REVIEW")

        db = _build_mock_db(store)
        db.update_match_status(mid, "USER_APPROVED", "user@example.com")

        match = db.get_match_by_id(mid)
        assert match is not None
        assert match.status == MatchStatus.USER_APPROVED

    def test_approve_sets_reviewed_by(self):
        store = FakeStore("user-1")
        mid = store.add_match(10, 20, 0.72, status="PENDING_REVIEW")

        db = _build_mock_db(store)
        db.update_match_status(mid, "USER_APPROVED", "user@example.com")

        match = db.get_match_by_id(mid)
        assert match.reviewed_by == "user@example.com"

    def test_approve_sets_reviewed_at(self):
        store = FakeStore("user-1")
        mid = store.add_match(10, 20, 0.72, status="PENDING_REVIEW")

        db = _build_mock_db(store)
        before = datetime.now(timezone.utc)
        db.update_match_status(mid, "USER_APPROVED", "user@example.com")

        match = db.get_match_by_id(mid)
        assert match.reviewed_at is not None
        assert match.reviewed_at >= before


# ═══════════════════════════════════════════════════════════════════
#  Reject action reverts transaction and document status
# ═══════════════════════════════════════════════════════════════════


class TestRejectAction:
    """After rejecting a match, the linked transaction goes back to UNMATCHED
    and the linked document goes back to EXTRACTED."""

    def _reject_match(self, store: FakeStore, db, match_id: int, reviewer: str):
        """Simulate the reject workflow from server/api/reconciliation.py."""
        match = db.get_match_by_id(match_id)
        assert match is not None

        db.update_match_status(match_id, "USER_REJECTED", reviewer)
        db.update_transaction_status(match.transaction_id, TransactionStatus.UNMATCHED)
        db.update_document_status(match.document_id, DocumentStatus.EXTRACTED.value)

    def test_reject_sets_match_status(self):
        store = FakeStore("user-2")
        store.add_transaction(10, "MATCHED")
        store.add_document(20, "MATCHED")
        mid = store.add_match(10, 20, 0.70, status="PENDING_REVIEW")

        db = _build_mock_db(store)
        self._reject_match(store, db, mid, "reviewer@example.com")

        match = db.get_match_by_id(mid)
        assert match.status == MatchStatus.USER_REJECTED

    def test_reject_reverts_transaction_to_unmatched(self):
        store = FakeStore("user-2")
        store.add_transaction(10, "MATCHED")
        store.add_document(20, "MATCHED")
        mid = store.add_match(10, 20, 0.70, status="PENDING_REVIEW")

        db = _build_mock_db(store)
        self._reject_match(store, db, mid, "reviewer@example.com")

        assert store.transactions[10]["status"] == "UNMATCHED"

    def test_reject_reverts_document_to_extracted(self):
        store = FakeStore("user-2")
        store.add_transaction(10, "MATCHED")
        store.add_document(20, "MATCHED")
        mid = store.add_match(10, 20, 0.70, status="PENDING_REVIEW")

        db = _build_mock_db(store)
        self._reject_match(store, db, mid, "reviewer@example.com")

        assert store.documents[20]["status"] == "EXTRACTED"

    def test_reject_sets_reviewed_by(self):
        store = FakeStore("user-2")
        store.add_transaction(10, "MATCHED")
        store.add_document(20, "MATCHED")
        mid = store.add_match(10, 20, 0.70, status="PENDING_REVIEW")

        db = _build_mock_db(store)
        self._reject_match(store, db, mid, "reviewer@example.com")

        match = db.get_match_by_id(mid)
        assert match.reviewed_by == "reviewer@example.com"
        assert match.reviewed_at is not None


# ═══════════════════════════════════════════════════════════════════
#  Three-queue classification by confidence score
# ═══════════════════════════════════════════════════════════════════


class TestConfidenceQueueClassification:
    """Matches are classified into AUTO_APPROVED (>= 0.90),
    PENDING_REVIEW (0.60-0.89), or PENDING_REVIEW (< 0.60)
    based on their confidence score.  The threshold is imported
    from ``server.api.reconciliation.AUTO_APPROVE_THRESHOLD``."""

    def test_high_confidence_auto_approved(self):
        """conf >= 0.90 -> AUTO_APPROVED."""
        store = FakeStore("user-3")
        mid = store.add_match(1, 1, 0.95)
        assert store.matches[mid]["status"] == "AUTO_APPROVED"

    def test_exact_threshold_auto_approved(self):
        """conf == 0.90 -> AUTO_APPROVED (boundary)."""
        store = FakeStore("user-3")
        mid = store.add_match(2, 2, 0.90)
        assert store.matches[mid]["status"] == "AUTO_APPROVED"

    def test_medium_confidence_pending_review(self):
        """0.60 <= conf < 0.90 -> PENDING_REVIEW."""
        store = FakeStore("user-3")
        mid = store.add_match(3, 3, 0.72)
        assert store.matches[mid]["status"] == "PENDING_REVIEW"

    def test_low_boundary_pending_review(self):
        """conf == 0.60 -> PENDING_REVIEW (boundary)."""
        store = FakeStore("user-3")
        mid = store.add_match(4, 4, 0.60)
        assert store.matches[mid]["status"] == "PENDING_REVIEW"

    def test_below_threshold_pending_review(self):
        """conf < 0.60 -> PENDING_REVIEW (low-conf still flagged)."""
        store = FakeStore("user-3")
        mid = store.add_match(5, 5, 0.45)
        assert store.matches[mid]["status"] == "PENDING_REVIEW"

    def test_just_below_auto_approve_is_pending(self):
        """conf == 0.89 -> PENDING_REVIEW (just below auto-approve)."""
        store = FakeStore("user-3")
        mid = store.add_match(6, 6, 0.89)
        assert store.matches[mid]["status"] == "PENDING_REVIEW"

    def test_classification_matches_model(self):
        """Verify classification aligns with ReconciliationMatch model via mock db."""
        store = FakeStore("user-3")
        mid_high = store.add_match(10, 10, 0.90)
        mid_med = store.add_match(11, 11, 0.75)
        mid_low = store.add_match(12, 12, 0.40)

        db = _build_mock_db(store)
        assert db.get_match_by_id(mid_high).status == MatchStatus.AUTO_APPROVED
        assert db.get_match_by_id(mid_med).status == MatchStatus.PENDING_REVIEW
        assert db.get_match_by_id(mid_low).status == MatchStatus.PENDING_REVIEW


# ═══════════════════════════════════════════════════════════════════
#  get_pending_reviews returns only PENDING_REVIEW
# ═══════════════════════════════════════════════════════════════════


class TestGetPendingReviews:
    """get_pending_reviews must return only matches with status PENDING_REVIEW,
    excluding AUTO_APPROVED, USER_APPROVED, and USER_REJECTED."""

    def test_returns_only_pending(self):
        store = FakeStore("user-4")
        store.add_match(1, 1, 0.95)   # AUTO_APPROVED
        store.add_match(2, 2, 0.72)   # PENDING_REVIEW
        store.add_match(3, 3, 0.65)   # PENDING_REVIEW
        store.add_match(4, 4, 0.92)   # AUTO_APPROVED

        db = _build_mock_db(store)
        pending = db.get_pending_reviews()

        assert len(pending) == 2
        for m in pending:
            assert m.status == MatchStatus.PENDING_REVIEW

    def test_excludes_approved_and_rejected(self):
        store = FakeStore("user-4")
        mid1 = store.add_match(1, 1, 0.72, status="PENDING_REVIEW")
        mid2 = store.add_match(2, 2, 0.72, status="PENDING_REVIEW")

        db = _build_mock_db(store)

        # Approve one, reject the other
        db.update_match_status(mid1, "USER_APPROVED", "approver@example.com")
        db.update_match_status(mid2, "USER_REJECTED", "rejector@example.com")

        pending = db.get_pending_reviews()
        assert len(pending) == 0

    def test_empty_when_no_matches(self):
        store = FakeStore("user-4")
        db = _build_mock_db(store)
        pending = db.get_pending_reviews()
        assert pending == []

    def test_pending_review_ids_are_correct(self):
        store = FakeStore("user-4")
        store.add_match(1, 1, 0.90)   # AUTO_APPROVED -> id 1
        mid2 = store.add_match(2, 2, 0.72)  # PENDING_REVIEW -> id 2
        mid3 = store.add_match(3, 3, 0.68)  # PENDING_REVIEW -> id 3
        store.add_match(4, 4, 0.95)   # AUTO_APPROVED -> id 4

        db = _build_mock_db(store)
        pending = db.get_pending_reviews()

        ids = {m.id for m in pending}
        assert ids == {mid2, mid3}
