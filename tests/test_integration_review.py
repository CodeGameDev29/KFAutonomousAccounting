"""Integration test: the review workflow, approve and reject.

Approving a match settles it as USER_APPROVED; rejecting it reverts the
transaction to UNMATCHED and the document to EXTRACTED, so both re-enter the
candidate pool on the next run.

Tests the chain:
    POST /api/transactions/{txn_id}/review  -->  approve  -->  verify status
    POST /api/transactions/{txn_id}/review  -->  reject   -->  verify revert
    POST /api/transactions/reviews/approve-all  -->  bulk approve

All database interactions use a mock DatabasePg. The review endpoint
executes raw SQL to find the pending match, then delegates to
db.update_match_status, db.update_transaction_status, and
db.update_document_status.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from models.match import MatchStatus, MatchType, ReconciliationMatch
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ── Constants ────────────────────────────────────────────────────────────

MOCK_USER = AuthUser(id="test-uuid-review", email="reviewer@example.com", role="user", email_verified=True)


# ── Helpers ──────────────────────────────────────────────────────────────

def _override_current_user():
    async def _inner():
        return MOCK_USER
    return _inner


def _make_pending_match(
    match_id: int,
    txn_id: int,
    doc_id: int,
    confidence: float = 0.85,
) -> ReconciliationMatch:
    """Create a PENDING_REVIEW match."""
    return ReconciliationMatch(
        id=match_id,
        transaction_id=txn_id,
        document_id=doc_id,
        confidence_score=confidence,
        amount_score=confidence,
        date_score=confidence,
        vendor_score=confidence,
        match_type=MatchType.ONE_TO_ONE,
        status=MatchStatus.PENDING_REVIEW,
    )


def _make_fake_db_with_pending_match(match_id, txn_id, doc_id):
    """Build a mock DatabasePg that simulates a pending match for a transaction.

    The review endpoint (POST /api/transactions/{txn_id}/review) does a raw SQL
    query inside db._conn() to find the pending match, so the whole context
    manager chain is mocked to return the expected (match_id, doc_id) row.
    """
    db = MagicMock()
    db.user_id = MOCK_USER.id

    # Track calls for assertions
    db.update_match_status = MagicMock()
    db.update_transaction_status = MagicMock()
    db.update_document_status = MagicMock()
    db.get_pending_reviews = MagicMock(return_value=[])

    # Mock the raw SQL path: db._conn() -> conn -> cursor -> execute -> fetchone
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (match_id, doc_id)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    db._conn.return_value = mock_conn_ctx

    return db


def _make_fake_db_for_bulk_approve(pending):
    """A mock DatabasePg for POST /reviews/approve-all.

    That endpoint reads ``db.get_pending_reviews()`` and then writes through a
    raw cursor, so the cursor is returned alongside the db: what it was asked
    to execute is the only record of which matches were settled.
    """
    db = MagicMock()
    db.user_id = MOCK_USER.id
    db.get_pending_reviews = MagicMock(return_value=list(pending))

    cursor = MagicMock()

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    db._conn.return_value = mock_conn_ctx

    return db, cursor


def _bulk_approved_ids(cursor) -> set[int]:
    """Match ids the endpoint moved to USER_APPROVED."""
    return {
        call.args[1][1]
        for call in cursor.execute.call_args_list
        if "UPDATE reconciliation_matches" in call.args[0]
    }


def _bulk_audited_ids(cursor) -> set[int]:
    """Match ids the endpoint wrote an audit_log row for."""
    return {
        call.args[1][1]
        for call in cursor.execute.call_args_list
        if "INSERT INTO audit_log" in call.args[0]
    }


def _make_fake_db_no_pending_match():
    """Build a mock DatabasePg that returns no pending match (fetchone = None)."""
    db = MagicMock()
    db.user_id = MOCK_USER.id

    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    db._conn.return_value = mock_conn_ctx

    return db


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_sse():
    """Suppress SSE event sending for all tests."""
    with patch("server.api.events.send_event", create=True):
        yield


@pytest.fixture()
def client():
    """FastAPI test client with auth overridden."""
    app.dependency_overrides[get_current_user] = _override_current_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Test: Approve match ──────────────────────────────────────────────


class TestApproveMatch:
    """POST /api/transactions/{txn_id}/review with action=approve changes
    the match status to USER_APPROVED via db.update_match_status."""

    def test_approve_returns_ok(self, client):
        """Approve returns 200 with action='approve' and the match_id."""
        db = _make_fake_db_with_pending_match(match_id=50, txn_id=101, doc_id=201)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post(
            "/api/transactions/101/review",
            json={"action": "approve"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "approve"
        assert data["match_id"] == 50

    def test_approve_calls_update_match_status_user_approved(self, client):
        """Approve calls db.update_match_status with USER_APPROVED."""
        db = _make_fake_db_with_pending_match(match_id=50, txn_id=101, doc_id=201)
        app.dependency_overrides[get_db] = lambda: db

        client.post(
            "/api/transactions/101/review",
            json={"action": "approve"},
        )

        db.update_match_status.assert_called_once_with(
            50, "USER_APPROVED", MOCK_USER.id
        )

    def test_approve_does_not_revert_transaction_or_document(self, client):
        """Approve does NOT call update_transaction_status or update_document_status."""
        db = _make_fake_db_with_pending_match(match_id=50, txn_id=101, doc_id=201)
        app.dependency_overrides[get_db] = lambda: db

        client.post(
            "/api/transactions/101/review",
            json={"action": "approve"},
        )

        db.update_transaction_status.assert_not_called()
        db.update_document_status.assert_not_called()


# ── Test: Reject match ───────────────────────────────────────────────


class TestRejectMatch:
    """POST /api/transactions/{txn_id}/review with action=reject changes
    match to USER_REJECTED and reverts transaction to UNMATCHED,
    document to EXTRACTED."""

    def test_reject_returns_ok(self, client):
        """Reject returns 200 with action='reject' and the match_id."""
        db = _make_fake_db_with_pending_match(match_id=60, txn_id=102, doc_id=202)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post(
            "/api/transactions/102/review",
            json={"action": "reject"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["action"] == "reject"
        assert data["match_id"] == 60

    def test_reject_calls_update_match_status_user_rejected(self, client):
        """Reject calls db.update_match_status with USER_REJECTED."""
        db = _make_fake_db_with_pending_match(match_id=60, txn_id=102, doc_id=202)
        app.dependency_overrides[get_db] = lambda: db

        client.post(
            "/api/transactions/102/review",
            json={"action": "reject"},
        )

        db.update_match_status.assert_called_once_with(
            60, "USER_REJECTED", MOCK_USER.id
        )

    def test_reject_reverts_transaction_to_unmatched(self, client):
        """Reject calls db.update_transaction_status(txn_id, 'UNMATCHED')."""
        db = _make_fake_db_with_pending_match(match_id=60, txn_id=102, doc_id=202)
        app.dependency_overrides[get_db] = lambda: db

        client.post(
            "/api/transactions/102/review",
            json={"action": "reject"},
        )

        db.update_transaction_status.assert_called_once_with(102, "UNMATCHED")

    def test_reject_reverts_document_to_extracted(self, client):
        """Reject calls db.update_document_status(doc_id, 'EXTRACTED')."""
        db = _make_fake_db_with_pending_match(match_id=60, txn_id=102, doc_id=202)
        app.dependency_overrides[get_db] = lambda: db

        client.post(
            "/api/transactions/102/review",
            json={"action": "reject"},
        )

        db.update_document_status.assert_called_once_with(202, "EXTRACTED")

    def test_reject_calls_all_three_updates_in_order(self, client):
        """Reject updates match, then transaction, then document -- all three called."""
        db = _make_fake_db_with_pending_match(match_id=60, txn_id=102, doc_id=202)
        app.dependency_overrides[get_db] = lambda: db

        client.post(
            "/api/transactions/102/review",
            json={"action": "reject"},
        )

        # All three must be called
        assert db.update_match_status.call_count == 1
        assert db.update_transaction_status.call_count == 1
        assert db.update_document_status.call_count == 1


# ── Test: Invalid action ─────────────────────────────────────────────


class TestInvalidReviewAction:
    """POST /api/transactions/{txn_id}/review with invalid action returns 400."""

    def test_invalid_action_returns_400(self, client):
        """An action other than 'approve' or 'reject' returns 400."""
        db = _make_fake_db_with_pending_match(match_id=70, txn_id=103, doc_id=203)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post(
            "/api/transactions/103/review",
            json={"action": "maybe"},
        )

        assert resp.status_code == 400
        assert "approve" in resp.json()["detail"].lower() or "reject" in resp.json()["detail"].lower()


# ── Test: No pending match ───────────────────────────────────────────


class TestNoPendingMatch:
    """POST /api/transactions/{txn_id}/review returns 404 when no pending match."""

    def test_no_pending_match_returns_404(self, client):
        """If no PENDING_REVIEW match exists for the transaction, return 404."""
        db = _make_fake_db_no_pending_match()
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post(
            "/api/transactions/999/review",
            json={"action": "approve"},
        )

        assert resp.status_code == 404
        assert "pending" in resp.json()["detail"].lower()


# ── Test: Approve then reject different transactions ─────────────────


class TestApproveAndRejectSequence:
    """Approve transaction 101, then reject transaction 102 -- verify
    independent status changes."""

    def test_approve_then_reject_different_transactions(self, client):
        """Approve txn 101, reject txn 102 -- both return ok with correct actions."""
        # First: approve txn 101
        db_approve = _make_fake_db_with_pending_match(match_id=50, txn_id=101, doc_id=201)
        app.dependency_overrides[get_db] = lambda: db_approve

        resp1 = client.post(
            "/api/transactions/101/review",
            json={"action": "approve"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["action"] == "approve"
        db_approve.update_match_status.assert_called_once_with(
            50, "USER_APPROVED", MOCK_USER.id
        )

        # Second: reject txn 102
        db_reject = _make_fake_db_with_pending_match(match_id=60, txn_id=102, doc_id=202)
        app.dependency_overrides[get_db] = lambda: db_reject

        resp2 = client.post(
            "/api/transactions/102/review",
            json={"action": "reject"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["action"] == "reject"
        db_reject.update_match_status.assert_called_once_with(
            60, "USER_REJECTED", MOCK_USER.id
        )
        db_reject.update_transaction_status.assert_called_once_with(102, "UNMATCHED")
        db_reject.update_document_status.assert_called_once_with(202, "EXTRACTED")


# ── Test: Bulk approve all ───────────────────────────────────────────


class TestBulkApproveAll:
    """POST /api/transactions/reviews/approve-all settles the confident matches.

    Unlike the single-transaction review above, this endpoint writes through a
    raw cursor rather than ``db.update_match_status``, and it applies the
    ``RECONCILIATION_BULK_APPROVE_MIN_CONFIDENCE`` floor: a bulk approval
    accepts a batch sight unseen, so it may only settle what the matcher was
    already confident about. ``tests/test_bulk_approve_threshold.py`` covers
    the threshold rules in detail; these cases cover the endpoint's place in
    the review workflow.
    """

    def test_approve_all_with_multiple_pending(self, client):
        """Three matches above the floor are all approved."""
        pending = [
            _make_pending_match(10, txn_id=101, doc_id=201, confidence=0.99),
            _make_pending_match(11, txn_id=102, doc_id=202, confidence=0.91),
            _make_pending_match(12, txn_id=103, doc_id=203, confidence=0.86),
        ]
        db, cursor = _make_fake_db_for_bulk_approve(pending)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/transactions/reviews/approve-all")

        assert resp.status_code == 200
        data = resp.json()
        assert data["approved"] == 3
        assert data["skipped_below_threshold"] == 0
        assert _bulk_approved_ids(cursor) == {10, 11, 12}

    def test_approve_all_writes_user_approved_for_each_match(self, client):
        """Every approved match gets its status row and its audit row."""
        pending = [
            _make_pending_match(10, txn_id=101, doc_id=201, confidence=0.95),
            _make_pending_match(11, txn_id=102, doc_id=202, confidence=0.95),
        ]
        db, cursor = _make_fake_db_for_bulk_approve(pending)
        app.dependency_overrides[get_db] = lambda: db

        client.post("/api/transactions/reviews/approve-all")

        statuses = [
            call.args[0]
            for call in cursor.execute.call_args_list
            if "UPDATE reconciliation_matches" in call.args[0]
        ]
        assert len(statuses) == 2
        assert all("USER_APPROVED" in sql for sql in statuses)
        assert _bulk_audited_ids(cursor) == {10, 11}

    def test_approve_all_leaves_unconfident_matches_pending(self, client):
        """A match below the floor is counted, never written, never audited."""
        pending = [
            _make_pending_match(10, txn_id=101, doc_id=201, confidence=0.99),
            _make_pending_match(11, txn_id=102, doc_id=202, confidence=0.78),
            _make_pending_match(12, txn_id=103, doc_id=203, confidence=0.82),
        ]
        db, cursor = _make_fake_db_for_bulk_approve(pending)
        app.dependency_overrides[get_db] = lambda: db

        data = client.post("/api/transactions/reviews/approve-all").json()

        assert data["approved"] == 1
        assert data["skipped_below_threshold"] == 2
        assert _bulk_approved_ids(cursor) == {10}
        assert _bulk_audited_ids(cursor) == {10}

    def test_approve_all_with_no_pending_returns_zero(self, client):
        """Approve-all with no pending matches returns approved=0."""
        db, cursor = _make_fake_db_for_bulk_approve([])
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/transactions/reviews/approve-all")

        assert resp.status_code == 200
        assert resp.json()["approved"] == 0
        assert cursor.execute.call_count == 0

    def test_approve_all_records_the_reviewer(self, client):
        """The authenticated user is written as reviewed_by and performed_by."""
        pending = [_make_pending_match(10, txn_id=101, doc_id=201, confidence=0.93)]
        db, cursor = _make_fake_db_for_bulk_approve(pending)
        app.dependency_overrides[get_db] = lambda: db

        client.post("/api/transactions/reviews/approve-all")

        update_params = [
            call.args[1]
            for call in cursor.execute.call_args_list
            if "UPDATE reconciliation_matches" in call.args[0]
        ]
        audit_params = [
            call.args[1]
            for call in cursor.execute.call_args_list
            if "INSERT INTO audit_log" in call.args[0]
        ]
        assert update_params == [(MOCK_USER.id, 10, MOCK_USER.id)]
        assert audit_params == [(MOCK_USER.id, 10, MOCK_USER.id)]

    def test_approve_all_reports_the_threshold_it_applied(self, client):
        """The response names the effective floor, and a body may raise it."""
        pending = [
            _make_pending_match(10, txn_id=101, doc_id=201, confidence=0.88),
            _make_pending_match(11, txn_id=102, doc_id=202, confidence=0.97),
        ]
        db, cursor = _make_fake_db_for_bulk_approve(pending)
        app.dependency_overrides[get_db] = lambda: db

        data = client.post(
            "/api/transactions/reviews/approve-all",
            json={"min_confidence": 0.95},
        ).json()

        assert data["min_confidence"] == 0.95
        assert data["approved"] == 1
        assert data["skipped_below_threshold"] == 1
        assert _bulk_approved_ids(cursor) == {11}
