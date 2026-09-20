"""Integration test: the full sync-to-reconcile pipeline.

Five synced transactions and three matching documents: reconciliation must
report three matches, fire the SSE event that refreshes the dashboard, and
leave the pending review visible in the action items.

Tests the chain:
    POST /api/actions/sync  -->  auto_reconcile  -->  SSE event  -->
    GET /api/dashboard/action-items

All external dependencies (Wise API, credential store, reconciliation engine,
SSE events, database) are mocked so the test runs without any services.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from models.document import Document, DocumentStatus
from models.match import MatchStatus, MatchType, ReconciliationMatch
from models.transaction import AccountType, Transaction, TransactionStatus
from server.app import app
from server.auth import AuthUser, get_current_user
from server.deps import get_db

# ── Constants ────────────────────────────────────────────────────────────

MOCK_USER = AuthUser(id="test-uuid-pipeline", email="pipeline@example.com", role="user", email_verified=True)


# ── Helpers ──────────────────────────────────────────────────────────────

def _override_current_user():
    async def _inner():
        return MOCK_USER
    return _inner


def _make_txn(txn_id: int, desc: str, amount: Decimal = Decimal("50.00")) -> Transaction:
    """Create a Transaction for testing."""
    return Transaction(
        id=txn_id,
        account=AccountType.CAD,
        transaction_type="DEBIT",
        date_posted=date(2026, 3, 15),
        amount=amount,
        currency="CAD",
        description=desc,
        source_file="wise_sync.csv",
        source_row=txn_id,
        status=TransactionStatus.UNMATCHED,
    )


def _make_doc(doc_id: int, vendor: str) -> Document:
    """Create a Document for testing."""
    return Document(
        id=doc_id,
        original_filename=f"{vendor.lower()}-invoice.pdf",
        file_hash=f"hash-{doc_id}",
        vendor=vendor,
        document_date=date(2026, 3, 14),
        currency="CAD",
        total=Decimal("50.00"),
        status=DocumentStatus.EXTRACTED,
    )


def _make_match(txn_id: int, doc_id: int, confidence: float) -> ReconciliationMatch:
    """Create a ReconciliationMatch."""
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
    """Mimics core.reconciliation.MultiPassResult."""

    def __init__(self, matches: list[ReconciliationMatch]):
        self.matches = matches
        self.pass_counts = {"pass_1_strict": len(matches)}


def _make_fake_db(
    unmatched_txns=None,
    unmatched_docs=None,
    pending_reviews=None,
    unmatched_for_action=None,
    onboarding_complete=True,
    gather_sources=None,
):
    """Return a mock DatabasePg with controllable return values."""
    db = MagicMock()
    db.user_id = MOCK_USER.id

    # Reconciliation-related
    db.get_unmatched_transactions = MagicMock(
        return_value=unmatched_txns if unmatched_txns is not None else []
    )
    db.get_unmatched_documents = MagicMock(
        return_value=unmatched_docs if unmatched_docs is not None else []
    )
    db.insert_match = MagicMock(return_value=1)
    db.update_transaction_status = MagicMock()
    db.update_document_status = MagicMock()

    # Sync-related
    db.insert_transaction = MagicMock()
    db.get_gather_sources = MagicMock(
        return_value=gather_sources if gather_sources is not None else []
    )

    # Dashboard action-items
    db.get_pending_reviews = MagicMock(
        return_value=pending_reviews if pending_reviews is not None else []
    )
    db.is_onboarding_complete = MagicMock(return_value=onboarding_complete)

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


# ── Test: Full sync → auto-reconcile pipeline ────────────────────────────


class TestSyncToReconcilePipeline:
    """Full pipeline: POST /api/actions/sync with auto_reconcile=true triggers
    Wise sync, inserts 5 transactions, runs reconciliation that matches 3,
    fires an SSE event, and dashboard action-items reflects pending reviews."""

    # Five transactions, three matching documents.
    TXNS = [
        _make_txn(1, "EXAMPLE RETAILER PURCHASE", Decimal("49.99")),
        _make_txn(2, "ORCHID TELECOM BILL", Decimal("85.00")),
        _make_txn(3, "BLUEPEAK SOFTWARE", Decimal("18.00")),
        _make_txn(4, "WISE TRANSFER TO CONTRACTOR", Decimal("500.00")),
        _make_txn(5, "MERIDIAN COURIER SERVICES", Decimal("32.50")),
    ]

    DOCS = [
        _make_doc(101, "Example Retailer"),
        _make_doc(102, "Orchid Telecom"),
        _make_doc(103, "Bluepeak Software"),
    ]

    # Reconciliation matches three of the five transactions to the three
    # documents: two above the threshold (auto-approved), one below it
    # (pending review).
    MATCHES = [
        _make_match(1, 101, 0.95),  # high confidence -> AUTO_APPROVED
        _make_match(2, 102, 0.92),  # high confidence -> AUTO_APPROVED
        _make_match(3, 103, 0.85),  # below threshold -> PENDING_REVIEW
    ]

    def _fake_wise_transactions(self):
        """Return 5 fake Wise transaction dicts."""
        return [
            {"date": "2026-03-15", "amount": -49.99, "currency": "CAD",
             "description": "EXAMPLE RETAILER PURCHASE"},
            {"date": "2026-03-15", "amount": -85.00, "currency": "CAD",
             "description": "ORCHID TELECOM BILL"},
            {"date": "2026-03-15", "amount": -18.00, "currency": "CAD",
             "description": "BLUEPEAK SOFTWARE"},
            {"date": "2026-03-15", "amount": -500.00, "currency": "CAD",
             "description": "WISE TRANSFER TO CONTRACTOR"},
            {"date": "2026-03-15", "amount": -32.50, "currency": "CAD",
             "description": "MERIDIAN COURIER SERVICES"},
        ]

    def test_sync_with_auto_reconcile_returns_matched_3(self, client):
        """POST /api/actions/sync returns Wise results and a reconciliation
        block reporting matched=3."""
        db = _make_fake_db(
            unmatched_txns=self.TXNS,
            unmatched_docs=self.DOCS,
        )
        app.dependency_overrides[get_db] = lambda: db

        fake_result = FakeMultiPassResult(matches=list(self.MATCHES))

        wise_txns_usd = self._fake_wise_transactions()[:3]
        wise_txns_cad = self._fake_wise_transactions()[3:]

        with (
            patch("core.wise_api.get_wise_token", return_value="fake-token"),
            patch("core.wise_api.get_profile_id", return_value="profile-123"),
            patch("core.wise_api.fetch_wise_transactions", side_effect=[
                wise_txns_usd,  # First call: USD
                wise_txns_cad,  # Second call: CAD
            ]),
            patch("core.credentials_cloud.CloudCredentialStore") as MockCredStore,
            patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result),
        ):
            mock_creds_instance = MockCredStore.return_value
            mock_creds_instance.retrieve.return_value = "fake-wise-token"

            resp = client.post(
                "/api/actions/sync",
                json={"sources": ["wise"], "days": 30, "auto_reconcile": True},
            )

        assert resp.status_code == 200
        data = resp.json()

        # Verify sync results contain Wise data
        assert data["status"] == "ok"
        results = data["results"]
        # Wise sync produces wise_USD and wise_CAD keys
        total_new = sum(
            v.get("new", 0) for v in results.values()
            if isinstance(v, dict) and "new" in v
        )
        assert total_new == 5, f"Expected 5 new transactions, got {total_new}"

        # Verify reconciliation ran and produced correct results
        recon = data["reconciliation"]
        assert recon is not None
        assert recon["matched"] == 3
        assert recon["auto_approved"] == 2
        assert recon["pending_review"] == 1

    def test_sync_fires_sse_reconciliation_complete(self, client):
        """Verify send_event is called with 'reconciliation_complete' after sync."""
        db = _make_fake_db(
            unmatched_txns=self.TXNS,
            unmatched_docs=self.DOCS,
        )
        app.dependency_overrides[get_db] = lambda: db

        fake_result = FakeMultiPassResult(matches=list(self.MATCHES))

        with (
            patch("core.wise_api.get_wise_token", return_value="fake-token"),
            patch("core.wise_api.get_profile_id", return_value="profile-123"),
            patch("core.wise_api.fetch_wise_transactions", return_value=self._fake_wise_transactions()[:3]),
            patch("core.credentials_cloud.CloudCredentialStore") as MockCredStore,
            patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result),
            patch("server.api.events.send_event") as mock_send_event,
        ):
            mock_creds_instance = MockCredStore.return_value
            mock_creds_instance.retrieve.return_value = "fake-wise-token"

            resp = client.post(
                "/api/actions/sync",
                json={"sources": ["wise"], "days": 30, "auto_reconcile": True},
            )

        assert resp.status_code == 200

        # Find the reconciliation_complete event in the send_event calls
        recon_calls = [
            c for c in mock_send_event.call_args_list
            if len(c.args) >= 2 and c.args[1] == "reconciliation_complete"
        ]
        assert len(recon_calls) >= 1, (
            f"Expected send_event('reconciliation_complete') to be called. "
            f"Actual calls: {mock_send_event.call_args_list}"
        )

        # Verify event payload includes match counts
        event_data = recon_calls[0].args[2]
        assert event_data["matched"] == 3
        assert event_data["auto_approved"] == 2
        assert event_data["pending_review"] == 1

    def test_action_items_reflect_pending_reviews(self, client):
        """GET /api/dashboard/action-items includes pending_reviews after reconciliation."""
        # Create a pending review match to return from get_pending_reviews
        pending_match = ReconciliationMatch(
            id=1,
            transaction_id=3,
            document_id=103,
            confidence_score=0.85,
            amount_score=0.85,
            date_score=0.85,
            vendor_score=0.85,
            match_type=MatchType.ONE_TO_ONE,
            status=MatchStatus.PENDING_REVIEW,
        )

        db = _make_fake_db(
            pending_reviews=[pending_match],
            unmatched_for_action=[],
            onboarding_complete=True,
            gather_sources=[{"source_type": "wise", "enabled": True}],
        )
        app.dependency_overrides[get_db] = lambda: db

        resp = client.get("/api/dashboard/action-items")

        assert resp.status_code == 200
        data = resp.json()
        items = data["items"]

        # Find the pending_reviews action item
        pending_items = [i for i in items if i["type"] == "pending_reviews"]
        assert len(pending_items) == 1
        assert pending_items[0]["count"] == 1
        assert "review" in pending_items[0]["message"].lower()

    def test_sync_no_auto_reconcile_when_flag_false(self, client):
        """POST /api/actions/sync with auto_reconcile=false skips reconciliation."""
        db = _make_fake_db()
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("core.wise_api.get_wise_token", return_value="fake-token"),
            patch("core.wise_api.get_profile_id", return_value="profile-123"),
            patch("core.wise_api.fetch_wise_transactions", return_value=[
                {"date": "2026-03-15", "amount": -10, "currency": "CAD",
                 "description": "TEST"},
            ]),
            patch("core.credentials_cloud.CloudCredentialStore") as MockCredStore,
            patch("core.reconciliation.find_matches_multi_pass") as mock_recon,
        ):
            mock_creds_instance = MockCredStore.return_value
            mock_creds_instance.retrieve.return_value = "fake-wise-token"

            resp = client.post(
                "/api/actions/sync",
                json={"sources": ["wise"], "days": 30, "auto_reconcile": False},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["reconciliation"] is None
        mock_recon.assert_not_called()

    def test_sync_skips_reconcile_when_no_new_transactions(self, client):
        """If all insert_transaction calls raise (duplicates), auto_reconcile is skipped."""
        db = _make_fake_db()
        # All inserts fail (duplicates)
        db.insert_transaction.side_effect = Exception("duplicate")
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("core.wise_api.get_wise_token", return_value="fake-token"),
            patch("core.wise_api.get_profile_id", return_value="profile-123"),
            patch("core.wise_api.fetch_wise_transactions", return_value=[
                {"date": "2026-03-15", "amount": -10, "currency": "CAD",
                 "description": "DUPE"},
            ]),
            patch("core.credentials_cloud.CloudCredentialStore") as MockCredStore,
            patch("core.reconciliation.find_matches_multi_pass") as mock_recon,
        ):
            mock_creds_instance = MockCredStore.return_value
            mock_creds_instance.retrieve.return_value = "fake-wise-token"

            resp = client.post(
                "/api/actions/sync",
                json={"sources": ["wise"], "days": 30, "auto_reconcile": True},
            )

        assert resp.status_code == 200
        data = resp.json()
        # new_count is 0 for all currencies since inserts failed
        assert data["reconciliation"] is None
        mock_recon.assert_not_called()

    def test_sync_reconciliation_result_contains_matches_detail(self, client):
        """Verify the reconciliation result contains per-match detail array."""
        db = _make_fake_db(
            unmatched_txns=self.TXNS,
            unmatched_docs=self.DOCS,
        )
        app.dependency_overrides[get_db] = lambda: db

        fake_result = FakeMultiPassResult(matches=list(self.MATCHES))

        with (
            patch("core.wise_api.get_wise_token", return_value="fake-token"),
            patch("core.wise_api.get_profile_id", return_value="profile-123"),
            patch("core.wise_api.fetch_wise_transactions", return_value=self._fake_wise_transactions()),
            patch("core.credentials_cloud.CloudCredentialStore") as MockCredStore,
            patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result),
        ):
            mock_creds_instance = MockCredStore.return_value
            mock_creds_instance.retrieve.return_value = "fake-wise-token"

            resp = client.post(
                "/api/actions/sync",
                json={"sources": ["wise"], "days": 30, "auto_reconcile": True},
            )

        assert resp.status_code == 200
        recon = resp.json()["reconciliation"]

        # Verify matches detail list
        assert len(recon["matches"]) == 3
        statuses = {m["status"] for m in recon["matches"]}
        assert "AUTO_APPROVED" in statuses
        assert "PENDING_REVIEW" in statuses

        # Verify each match has expected fields
        for m in recon["matches"]:
            assert "transaction_id" in m
            assert "document_id" in m
            assert "confidence" in m
            assert "status" in m
            assert "match_type" in m

    def test_sync_db_insert_called_for_each_transaction(self, client):
        """Verify db.insert_transaction is called once per Wise transaction."""
        db = _make_fake_db(
            unmatched_txns=[],
            unmatched_docs=[],
        )
        app.dependency_overrides[get_db] = lambda: db

        fake_result = FakeMultiPassResult(matches=[])

        wise_txns = self._fake_wise_transactions()

        with (
            patch("core.wise_api.get_wise_token", return_value="fake-token"),
            patch("core.wise_api.get_profile_id", return_value="profile-123"),
            patch("core.wise_api.fetch_wise_transactions", side_effect=[
                wise_txns[:3],  # USD batch
                wise_txns[3:],  # CAD batch
            ]),
            patch("core.credentials_cloud.CloudCredentialStore") as MockCredStore,
            patch("core.reconciliation.find_matches_multi_pass", return_value=fake_result),
        ):
            mock_creds_instance = MockCredStore.return_value
            mock_creds_instance.retrieve.return_value = "fake-wise-token"

            resp = client.post(
                "/api/actions/sync",
                json={"sources": ["wise"], "days": 30, "auto_reconcile": True},
            )

        assert resp.status_code == 200
        assert db.insert_transaction.call_count == 5

    def test_sync_wise_no_token_returns_skipped(self, client):
        """If Wise token is not configured, sync returns skipped for Wise."""
        db = _make_fake_db()
        app.dependency_overrides[get_db] = lambda: db

        with (
            patch("core.credentials_cloud.CloudCredentialStore") as MockCredStore,
        ):
            mock_creds_instance = MockCredStore.return_value
            mock_creds_instance.retrieve.return_value = None

            resp = client.post(
                "/api/actions/sync",
                json={"sources": ["wise"], "days": 30, "auto_reconcile": True},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "wise" in data["results"]
        assert "skipped" in data["results"]["wise"]
