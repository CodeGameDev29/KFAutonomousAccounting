"""Closing and reopening a month, and the states that must be refused.

Closing a month freezes it for filing, so the endpoints have to refuse the
transitions that would make the frozen figures meaningless: closing a month
that is already closed, closing a month with nothing in it, reopening a month
that was never closed, or naming a month outside 1-12. The close response also
reports a match rate, and IGNORED rows — the ones that need no receipt — are
left out of its denominator so the rate describes work still to do.

All database methods are mocked, so the tests run without PostgreSQL.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from models.transaction import AccountType, Transaction, TransactionStatus
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ── Helpers ──────────────────────────────────────────────────────────────

MOCK_USER = AuthUser(id="test-uuid-month", email="month@example.com", role="user", email_verified=True)


def _override_current_user():
    async def _inner():
        return MOCK_USER
    return _inner


def _make_txn(txn_id: int, status: str = "MATCHED") -> Transaction:
    """Create a minimal Transaction for testing."""
    return Transaction(
        id=txn_id,
        account=AccountType.CAD,
        transaction_type="DEBIT",
        date_posted=date(2026, 3, 15),
        amount=Decimal("100.00"),
        currency="CAD",
        description="Test transaction",
        source_file="test.csv",
        source_row=txn_id,
        status=TransactionStatus(status),
    )


def _make_fake_db(
    *,
    is_closed: bool = False,
    transactions: list[Transaction] | None = None,
    month_statuses: dict | None = None,
):
    """Return a mock DatabasePg for month lifecycle tests."""
    db = MagicMock()
    db.user_id = MOCK_USER.id
    db.is_month_closed = MagicMock(return_value=is_closed)
    db.get_transactions_by_month = MagicMock(return_value=transactions or [])
    db.close_month = MagicMock()
    db.reopen_month = MagicMock()
    db.get_month_statuses = MagicMock(
        return_value=month_statuses or {}
    )
    return db


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _override_auth():
    """Override auth for all tests in this module."""
    app.dependency_overrides[get_current_user] = _override_current_user()
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client():
    return TestClient(app)


# ── Close month ─────────────────────────────────────────────────────────

class TestCloseMonth:
    """POST /api/reports/{year}/{month}/close — freeze a month, or refuse to."""

    def test_close_month_success(self, client):
        """Close a month that is open and has transactions -> 200."""
        txns = [_make_txn(1, "MATCHED"), _make_txn(2, "UNMATCHED")]
        db = _make_fake_db(is_closed=False, transactions=txns)
        # get_month_statuses returns close info for the month_report call
        db.get_month_statuses.return_value = {}
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/3/close")

        assert resp.status_code == 200
        data = resp.json()
        assert data["closed"] is True
        assert data["year"] == 2026
        assert data["month"] == 3
        assert data["label"] == "Mar2026"
        assert data["transaction_count"] == 2
        # 1 matched out of 2 reconcilable (none IGNORED)
        assert data["matched_count"] == 1
        assert data["unmatched_count"] == 1

        # Verify db.close_month was called with correct args
        db.close_month.assert_called_once_with(2026, 3, closed_by=MOCK_USER.id)

    def test_close_month_already_closed_409(self, client):
        """Closing an already-closed month -> 409 Conflict."""
        db = _make_fake_db(is_closed=True)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/3/close")

        assert resp.status_code == 409
        assert "already closed" in resp.json()["detail"]

    def test_close_month_no_transactions_422(self, client):
        """Closing a month with 0 transactions -> 422."""
        db = _make_fake_db(is_closed=False, transactions=[])
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/3/close")

        assert resp.status_code == 422
        assert "0 transactions" in resp.json()["detail"]

    def test_close_month_invalid_month_400(self, client):
        """Month=13 -> 400 Bad Request."""
        db = _make_fake_db()
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/13/close")

        assert resp.status_code == 400
        assert "1-12" in resp.json()["detail"]

    def test_close_month_match_rate_calculation(self, client):
        """Match rate calculation: IGNORED txns excluded from rate denominator."""
        txns = [
            _make_txn(1, "MATCHED"),
            _make_txn(2, "MATCHED"),
            _make_txn(3, "UNMATCHED"),
            _make_txn(4, "IGNORED"),
        ]
        db = _make_fake_db(is_closed=False, transactions=txns)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/3/close")

        assert resp.status_code == 200
        data = resp.json()
        # 2 matched out of 3 reconcilable (4 total - 1 IGNORED = 3)
        assert data["matched_count"] == 2
        # match_rate = 2/3 * 100 = 66.7
        assert data["match_rate"] == 66.7


# ── Reopen month ───────────────────────────────────────────────────────

class TestReopenMonth:
    """POST /api/reports/{year}/{month}/reopen — unfreeze one that is closed."""

    def test_reopen_closed_month_success(self, client):
        """Reopen a closed month -> 200 with closed=False."""
        db = _make_fake_db(is_closed=True)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/3/reopen")

        assert resp.status_code == 200
        data = resp.json()
        assert data["closed"] is False
        assert data["year"] == 2026
        assert data["month"] == 3
        assert data["label"] == "Mar2026"
        assert "reopened_at" in data

        db.reopen_month.assert_called_once_with(2026, 3)

    def test_reopen_already_open_409(self, client):
        """Reopening a month that is not closed -> 409 Conflict."""
        db = _make_fake_db(is_closed=False)
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/3/reopen")

        assert resp.status_code == 409
        assert "not closed" in resp.json()["detail"]

    def test_reopen_invalid_month_400(self, client):
        """Month=0 -> 400 Bad Request."""
        db = _make_fake_db()
        app.dependency_overrides[get_db] = lambda: db

        resp = client.post("/api/reports/2026/0/reopen")

        assert resp.status_code == 400
        assert "1-12" in resp.json()["detail"]


# ── Round-trip test ─────────────────────────────────────────────────────

class TestCloseReopenRoundTrip:
    """Verify a full close -> reopen lifecycle."""

    def test_close_then_reopen(self, client):
        """Close a month, then reopen it. Both succeed."""
        txns = [_make_txn(1, "MATCHED")]

        # Step 1: Close
        db_open = _make_fake_db(is_closed=False, transactions=txns)
        app.dependency_overrides[get_db] = lambda: db_open

        resp = client.post("/api/reports/2026/1/close")
        assert resp.status_code == 200
        assert resp.json()["closed"] is True

        # Step 2: Reopen
        db_closed = _make_fake_db(is_closed=True)
        app.dependency_overrides[get_db] = lambda: db_closed

        resp = client.post("/api/reports/2026/1/reopen")
        assert resp.status_code == 200
        assert resp.json()["closed"] is False
