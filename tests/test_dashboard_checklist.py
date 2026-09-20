"""Tests for the dashboard checklist and its action items.

Covers the action-items endpoint and the SSE events the receipt and
reconciliation handlers emit, driven against the real handlers.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ---------------------------------------------------------------------------
# Fixtures: mock auth + mock db injected via FastAPI dependency overrides
# ---------------------------------------------------------------------------

MOCK_USER = AuthUser(id="test-user-uuid-1234", email="test@example.com", role="authenticated", email_verified=True)


def _override_get_current_user():
    """Return a mock authenticated user (replaces real JWT verification)."""
    return MOCK_USER


@pytest.fixture(autouse=True)
def _override_auth():
    """Override auth dependency for every test in this module."""
    app.dependency_overrides[get_current_user] = _override_get_current_user
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def mock_db():
    """Create a mock DatabasePg and register it as a dependency override."""
    db = MagicMock()
    db.user_id = MOCK_USER.id

    # Defaults: empty lists / False so tests opt-in to specific behaviors
    db.is_onboarding_complete.return_value = False
    db.get_unmatched_transactions.return_value = []
    db.get_pending_reviews.return_value = []
    db.get_gather_sources.return_value = []

    app.dependency_overrides[get_db] = lambda: db
    yield db
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def client():
    """Synchronous test client for the FastAPI app."""
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helper: build a lightweight mock transaction
# ---------------------------------------------------------------------------

def _mock_transaction(id_: int = 1, description: str = "SOME VENDOR PURCHASE"):
    txn = MagicMock()
    txn.id = id_
    txn.description = description
    return txn


def _mock_match(id_: int = 1, confidence: float = 0.85):
    m = MagicMock()
    m.id = id_
    m.confidence_score = confidence
    return m


# ---------------------------------------------------------------------------
# action-items returns an onboarding item when onboarding is incomplete
# ---------------------------------------------------------------------------

class TestActionItemsOnboarding:
    """GET /api/dashboard/action-items returns an item with type=='onboarding'
    when db.is_onboarding_complete() returns False.
    """

    def test_onboarding_incomplete_returns_onboarding_item(self, client, mock_db):
        mock_db.is_onboarding_complete.return_value = False

        resp = client.get("/api/dashboard/action-items")
        assert resp.status_code == 200

        items = resp.json()["items"]
        onboarding_items = [i for i in items if i["type"] == "onboarding"]
        assert len(onboarding_items) == 1, (
            f"Expected exactly 1 onboarding item, got {len(onboarding_items)}: {items}"
        )
        assert onboarding_items[0]["action"] == "/onboarding"

    def test_onboarding_complete_no_onboarding_item(self, client, mock_db):
        mock_db.is_onboarding_complete.return_value = True

        resp = client.get("/api/dashboard/action-items")
        assert resp.status_code == 200

        items = resp.json()["items"]
        onboarding_items = [i for i in items if i["type"] == "onboarding"]
        assert len(onboarding_items) == 0, (
            f"Expected 0 onboarding items when complete, got: {onboarding_items}"
        )


# ---------------------------------------------------------------------------
# action-items returns missing_receipts when transactions are unmatched
# ---------------------------------------------------------------------------

class TestActionItemsMissingReceipts:
    """GET /api/dashboard/action-items returns an item with
    type=='missing_receipts' when db.get_unmatched_transactions() returns
    a non-empty list.
    """

    def test_unmatched_transactions_returns_missing_receipts(self, client, mock_db):
        mock_db.is_onboarding_complete.return_value = True
        mock_db.get_unmatched_transactions.return_value = [
            _mock_transaction(1, "ORCHID TELECOM"),
            _mock_transaction(2, "EXAMPLE RETAILER"),
            _mock_transaction(3, "BLUEPEAK SOFTWARE"),
        ]

        resp = client.get("/api/dashboard/action-items")
        assert resp.status_code == 200

        items = resp.json()["items"]
        missing = [i for i in items if i["type"] == "missing_receipts"]
        assert len(missing) == 1, f"Expected 1 missing_receipts item, got: {items}"
        assert missing[0]["count"] == 3

    def test_no_unmatched_transactions_no_missing_receipts(self, client, mock_db):
        mock_db.is_onboarding_complete.return_value = True
        mock_db.get_unmatched_transactions.return_value = []

        resp = client.get("/api/dashboard/action-items")
        assert resp.status_code == 200

        items = resp.json()["items"]
        missing = [i for i in items if i["type"] == "missing_receipts"]
        assert len(missing) == 0, (
            f"Expected no missing_receipts when list is empty, got: {missing}"
        )


# ---------------------------------------------------------------------------
# action-items returns pending_reviews when matches exist
# ---------------------------------------------------------------------------

class TestActionItemsPendingReviews:
    """GET /api/dashboard/action-items returns an item with
    type=='pending_reviews' when db.get_pending_reviews() returns matches.
    """

    def test_pending_reviews_returned(self, client, mock_db):
        mock_db.is_onboarding_complete.return_value = True
        mock_db.get_pending_reviews.return_value = [
            _mock_match(1, 0.72),
            _mock_match(2, 0.65),
        ]

        resp = client.get("/api/dashboard/action-items")
        assert resp.status_code == 200

        items = resp.json()["items"]
        pending = [i for i in items if i["type"] == "pending_reviews"]
        assert len(pending) == 1, f"Expected 1 pending_reviews item, got: {items}"
        assert pending[0]["count"] == 2
        assert "/transactions?filter=pending_review" in pending[0]["action"]

    def test_no_pending_reviews_no_item(self, client, mock_db):
        mock_db.is_onboarding_complete.return_value = True
        mock_db.get_pending_reviews.return_value = []

        resp = client.get("/api/dashboard/action-items")
        assert resp.status_code == 200

        items = resp.json()["items"]
        pending = [i for i in items if i["type"] == "pending_reviews"]
        assert len(pending) == 0


# ---------------------------------------------------------------------------
# send_event is called with "receipts_updated" after a receipt upload
# ---------------------------------------------------------------------------

class TestReceiptUploadSendsEvent:
    """After a successful receipt upload, send_event is called with
    'receipts_updated'.
    """

    def test_send_event_receipts_updated(self, client, mock_db):
        mock_db.is_file_processed.return_value = False
        mock_db.insert_processed_file.return_value = None
        mock_db.insert_document.return_value = 42  # doc_id

        # Mock the _conn context manager for the field_confidence UPDATE
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_db._conn.return_value = mock_conn

        # Build a fake extraction result
        fake_extraction = {
            "vendor": "Example Retailer",
            "date": "2026-01-15",
            "total": "59.99",
            "currency": "CAD",
            "subtotal": "53.09",
            "tax_gst": "0.00",
            "tax_hst": "6.90",
            "tax_pst": "0.00",
            "tax_other": "0.00",
            "line_items": [],
            "payment_method": "Visa",
            "invoice_number": "INV-001",
            "_provider": "gemini",
        }

        fake_doc = MagicMock()
        fake_doc.vendor = "Example Retailer"
        fake_doc.document_date = MagicMock()
        fake_doc.document_date.isoformat.return_value = "2026-01-15"
        fake_doc.total = 59.99
        fake_doc.currency = "CAD"
        fake_doc.extraction_confidence = 0.95
        fake_doc.stored_path = "/tmp/test.pdf"

        with patch("core.extraction.extract_document", new_callable=AsyncMock, return_value=fake_extraction) as mock_extract, \
             patch("core.extraction.build_document_from_extraction", return_value=fake_doc), \
             patch("core.extraction.compute_field_confidence", return_value={"vendor": "high", "date": "high", "total": "high"}), \
             patch("server.api.events.send_event") as mock_send_event:

            # Upload a minimal PDF (content doesn't matter since extraction is mocked)
            resp = client.post(
                "/api/receipts/upload",
                files={"file": ("receipt.pdf", b"%PDF-1.4 fake content", "application/pdf")},
            )

            assert resp.status_code == 200, f"Upload failed: {resp.json()}"
            data = resp.json()
            assert data["status"] == "ok"
            assert data["doc_id"] == 42

            # Verify send_event was called with receipts_updated
            mock_send_event.assert_called_once_with(
                MOCK_USER.id,
                "receipts_updated",
                {"doc_id": 42},
            )


# ---------------------------------------------------------------------------
# send_event is called with "reconciliation_complete" after reconciliation
# ---------------------------------------------------------------------------

class TestReconciliationSendsEvent:
    """After reconciliation completes, send_event is called with
    'reconciliation_complete'.
    """

    def test_send_event_reconciliation_complete(self, client, mock_db):
        # Set up mock db to return some transactions and documents
        mock_txn = MagicMock()
        mock_txn.id = 1
        mock_txn.description = "ORCHID TELECOM"
        mock_db.get_unmatched_transactions.return_value = [mock_txn]
        mock_db.get_unmatched_documents.return_value = []
        mock_db.update_transaction_status.return_value = None
        mock_db.insert_match.return_value = None
        mock_db.update_document_status.return_value = None

        # Mock the reconciliation engine to return an empty result
        mock_mp_result = MagicMock()
        mock_mp_result.matches = []
        mock_mp_result.pass_counts = {"pass_1": 0, "pass_2": 0, "pass_3": 0}

        with patch("core.reconciliation.find_matches_multi_pass", return_value=mock_mp_result), \
             patch("server.api.events.send_event") as mock_send_event:

            resp = client.post("/api/reconciliation/run")

            assert resp.status_code == 200, f"Reconciliation failed: {resp.json()}"
            data = resp.json()
            assert "matched" in data

            # Verify send_event was called with reconciliation_complete
            mock_send_event.assert_called_once_with(
                MOCK_USER.id,
                "reconciliation_complete",
                {
                    "matched": 0,
                    "auto_approved": 0,
                    "pending_review": 0,
                    "rate": 0.0,
                },
            )

    def test_send_event_with_actual_matches(self, client, mock_db):
        """Verify that reconciliation_complete event includes correct counts
        when matches are found."""
        mock_txn_1 = MagicMock()
        mock_txn_1.id = 1
        mock_txn_1.description = "ORCHID TELECOM"

        mock_txn_2 = MagicMock()
        mock_txn_2.id = 2
        mock_txn_2.description = "EXAMPLE RETAILER PURCHASE"

        mock_db.get_unmatched_transactions.return_value = [mock_txn_1, mock_txn_2]
        mock_db.get_unmatched_documents.return_value = [MagicMock(), MagicMock()]
        mock_db.update_transaction_status.return_value = None
        mock_db.insert_match.return_value = None
        mock_db.update_document_status.return_value = None

        # Create mock matches from the engine
        from models.match import MatchStatus

        mock_match_1 = MagicMock()
        mock_match_1.transaction_id = 1
        mock_match_1.document_id = 10
        mock_match_1.confidence_score = 0.95
        mock_match_1.status = MatchStatus("PENDING_REVIEW")
        mock_match_1.match_type = MagicMock()
        mock_match_1.match_type.value = "EXACT"

        mock_match_2 = MagicMock()
        mock_match_2.transaction_id = 2
        mock_match_2.document_id = 20
        mock_match_2.confidence_score = 0.70
        mock_match_2.status = MatchStatus("PENDING_REVIEW")
        mock_match_2.match_type = MagicMock()
        mock_match_2.match_type.value = "FUZZY"

        mock_mp_result = MagicMock()
        mock_mp_result.matches = [mock_match_1, mock_match_2]
        mock_mp_result.pass_counts = {"pass_1": 1, "pass_2": 1, "pass_3": 0}

        with patch("core.reconciliation.find_matches_multi_pass", return_value=mock_mp_result), \
             patch("server.api.events.send_event") as mock_send_event:

            resp = client.post("/api/reconciliation/run")
            assert resp.status_code == 200

            data = resp.json()
            assert data["matched"] == 2

            # One match above 0.90 threshold -> auto approved,
            # one below -> pending review
            mock_send_event.assert_called_once()
            call_args = mock_send_event.call_args
            assert call_args[0][0] == MOCK_USER.id
            assert call_args[0][1] == "reconciliation_complete"
            event_data = call_args[0][2]
            assert event_data["matched"] == 2
            assert event_data["auto_approved"] == 1
            assert event_data["pending_review"] == 1
            assert event_data["rate"] == 100.0
