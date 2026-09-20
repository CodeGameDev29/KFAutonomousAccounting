"""Tests for reconciliation's auto-approve threshold.

Verifies that _run_reconciliation_internal correctly classifies matches as
AUTO_APPROVED or PENDING_REVIEW based on confidence scores, and that the
AUTO_APPROVE_THRESHOLD constant is 0.90.

All reconciliation engine calls and database methods are mocked so these
tests run without external services.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, Transaction, TransactionStatus
from server.api.reconciliation import AUTO_APPROVE_THRESHOLD
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ── Helpers ──────────────────────────────────────────────────────────────

MOCK_USER = AuthUser(id="test-uuid-recon", email="recon@example.com", role="user", email_verified=True)


def _override_current_user():
    async def _inner():
        return MOCK_USER
    return _inner


def _make_txn(txn_id: int, desc: str = "EXAMPLE RETAILER PURCHASE") -> Transaction:
    """Create a minimal Transaction for testing."""
    return Transaction(
        id=txn_id,
        account=AccountType.CAD,
        transaction_type="DEBIT",
        date_posted=date(2026, 3, 15),
        amount=Decimal("49.99"),
        currency="CAD",
        description=desc,
        source_file="test.csv",
        source_row=1,
        status=TransactionStatus.UNMATCHED,
    )


def _make_match(txn_id: int, doc_id: int, confidence: float) -> ReconciliationMatch:
    """Create a ReconciliationMatch with a given confidence score."""
    return ReconciliationMatch(
        transaction_id=txn_id,
        document_id=doc_id,
        confidence_score=confidence,
        amount_score=confidence,
        date_score=confidence,
        vendor_score=confidence,
        match_type=MatchType.ONE_TO_ONE,
        status=MatchStatus.PENDING_REVIEW,
    )


class FakeMultiPassResult:
    """Mimics core.reconciliation.MultiPassResult for controlled test output."""

    def __init__(self, matches: list[ReconciliationMatch]):
        self.matches = matches
        self.pass_counts = {"pass_1_strict": len(matches)}


def _make_fake_db(unmatched_txns=None, unmatched_docs=None):
    """Return a mock DatabasePg with controllable return values."""
    db = MagicMock()
    db.user_id = MOCK_USER.id
    db.get_unmatched_transactions = MagicMock(return_value=unmatched_txns or [])
    db.get_unmatched_documents = MagicMock(return_value=unmatched_docs or [])
    db.insert_match = MagicMock(return_value=1)
    db.update_transaction_status = MagicMock()
    db.update_document_status = MagicMock()
    return db


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_sse():
    """Suppress SSE event sending for all tests."""
    with patch("server.api.events.send_event", create=True):
        yield


@pytest.fixture()
def client():
    """FastAPI test client with auth and db overridden."""
    app.dependency_overrides[get_current_user] = _override_current_user()
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Tests ────────────────────────────────────────────────────────────────

def test_auto_approve_threshold_constant():
    """AUTO_APPROVE_THRESHOLD must equal 0.90."""
    assert AUTO_APPROVE_THRESHOLD == 0.90


@pytest.mark.asyncio
async def test_high_confidence_is_auto_approved():
    """A match with confidence > 0.90 is marked AUTO_APPROVED."""
    txn = _make_txn(1)
    high_match = _make_match(1, 100, 0.95)
    fake_result = FakeMultiPassResult(matches=[high_match])

    db = _make_fake_db(unmatched_txns=[txn], unmatched_docs=[])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result):
        from server.api.reconciliation import _run_reconciliation_internal
        result = await _run_reconciliation_internal(MOCK_USER, db)

    assert result["matched"] == 1
    assert result["auto_approved"] == 1
    assert result["pending_review"] == 0

    # Verify the match object was mutated to AUTO_APPROVED before insertion
    inserted_match = db.insert_match.call_args[0][0]
    assert inserted_match.status == MatchStatus.AUTO_APPROVED


@pytest.mark.asyncio
async def test_low_confidence_goes_to_pending_review():
    """A match with confidence <= 0.90 is marked PENDING_REVIEW."""
    txn = _make_txn(1)
    low_match = _make_match(1, 100, 0.85)
    fake_result = FakeMultiPassResult(matches=[low_match])

    db = _make_fake_db(unmatched_txns=[txn], unmatched_docs=[])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result):
        from server.api.reconciliation import _run_reconciliation_internal
        result = await _run_reconciliation_internal(MOCK_USER, db)

    assert result["matched"] == 1
    assert result["auto_approved"] == 0
    assert result["pending_review"] == 1

    inserted_match = db.insert_match.call_args[0][0]
    assert inserted_match.status == MatchStatus.PENDING_REVIEW


@pytest.mark.asyncio
async def test_no_matches_reports_zero_results():
    """When no data matches, result has 0 matches and 0% rate."""
    txn = _make_txn(1)
    empty_result = FakeMultiPassResult(matches=[])

    db = _make_fake_db(unmatched_txns=[txn], unmatched_docs=[])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=empty_result):
        from server.api.reconciliation import _run_reconciliation_internal
        result = await _run_reconciliation_internal(MOCK_USER, db)

    assert result["matched"] == 0
    assert result["auto_approved"] == 0
    assert result["pending_review"] == 0
    assert result["rate"] == 0.0
    assert result["matches"] == []


@pytest.mark.asyncio
async def test_mixed_confidence_split():
    """Mixed confidences: one above threshold, one below. Verify split."""
    txn1 = _make_txn(1, "ORCHID TELECOM BILL")
    txn2 = _make_txn(2, "EXAMPLE RETAILER ORDER")
    high = _make_match(1, 100, 0.95)
    low = _make_match(2, 200, 0.88)
    fake_result = FakeMultiPassResult(matches=[high, low])

    db = _make_fake_db(unmatched_txns=[txn1, txn2], unmatched_docs=[])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result):
        from server.api.reconciliation import _run_reconciliation_internal
        result = await _run_reconciliation_internal(MOCK_USER, db)

    assert result["matched"] == 2
    assert result["auto_approved"] == 1
    assert result["pending_review"] == 1


@pytest.mark.asyncio
async def test_non_reconcilable_excluded():
    """Non-reconcilable transactions (bank fees etc.) are IGNORED, not matched."""
    fee_txn = _make_txn(1, "MONTHLY FEE")
    real_txn = _make_txn(2, "EXAMPLE RETAILER PURCHASE")
    match = _make_match(2, 100, 0.95)
    fake_result = FakeMultiPassResult(matches=[match])

    db = _make_fake_db(unmatched_txns=[fee_txn, real_txn], unmatched_docs=[])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result):
        from server.api.reconciliation import _run_reconciliation_internal
        result = await _run_reconciliation_internal(MOCK_USER, db)

    assert result["non_reconcilable"] == 1
    assert result["matched"] == 1
    # Bank fee should be set to IGNORED
    db.update_transaction_status.assert_any_call(1, "IGNORED")


def test_reconcile_endpoint_via_testclient(client):
    """POST /api/reconciliation/run returns proper JSON via TestClient."""
    txn = _make_txn(1)
    match = _make_match(1, 100, 0.92)
    fake_result = FakeMultiPassResult(matches=[match])
    db = _make_fake_db(unmatched_txns=[txn], unmatched_docs=[])
    app.dependency_overrides[get_db] = lambda: db

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result):
        resp = client.post("/api/reconciliation/run")

    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] == 1
    assert data["auto_approved"] == 1
    assert len(data["matches"]) == 1
    assert data["matches"][0]["status"] == "AUTO_APPROVED"

    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = _override_current_user()


@pytest.mark.asyncio
async def test_boundary_exactly_threshold():
    """A match at exactly 0.90 should be PENDING_REVIEW (threshold is strict >)."""
    txn = _make_txn(1)
    boundary_match = _make_match(1, 100, 0.90)
    fake_result = FakeMultiPassResult(matches=[boundary_match])

    db = _make_fake_db(unmatched_txns=[txn], unmatched_docs=[])

    with patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result):
        from server.api.reconciliation import _run_reconciliation_internal
        result = await _run_reconciliation_internal(MOCK_USER, db)

    # 0.90 is NOT > 0.90, so it should be PENDING_REVIEW
    assert result["pending_review"] == 1
    assert result["auto_approved"] == 0
